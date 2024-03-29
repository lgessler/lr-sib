import itertools
from itertools import chain, repeat
from typing import Optional

import datasets
import torch
from datasets import ClassLabel, Dataset, DatasetDict, IterableDatasetDict, Sequence, Value
from tango import Step
from tango.common import Lazy, Tqdm
from tango.integrations.datasets import DatasetsFormat
from tango.integrations.transformers import Tokenizer
from transformers import DataCollatorForLanguageModeling


def ncycles(iterable, n):
    "Returns the sequence elements n times"
    return chain.from_iterable(repeat(tuple(iterable), n))


@Step.register("loreiba.sgcl.data::finalize")
class Finalize(Step):
    DETERMINISTIC = True
    CACHEABLE = True
    FORMAT = DatasetsFormat()

    def _get_labels(self, dataset, treebank_dataset):
        deprels = set()
        xpos = set()
        for split in ["train", "dev"]:
            for d in dataset[split]:
                for x in d["xpos"]:
                    xpos.add(x)
                for x in d["deprel"]:
                    deprels.add(x)
        if treebank_dataset is not None:
            for split in ["train", "dev", "test"]:
                xpos |= set(d for s in treebank_dataset[split]["xpos"] for d in s)
                deprels |= set(d for s in treebank_dataset[split]["deprel"] for d in s)
        xpos = sorted(list(xpos))
        deprels = sorted(list(deprels))
        self.logger.info(f"Using deprel set: {deprels}")
        self.logger.info(f"Using xpos set: {xpos}")
        return xpos, deprels

    # Note: we are expecting "full conllu" in the dataset argument here, as would be produced by
    # loreiba.data.stanza::stanza_parse_dataset
    def run(
        self,
        dataset: IterableDatasetDict,
        treebank_dataset: Optional[DatasetDict] = None,
        unlabeled_per_labeled: int = 8,
    ) -> DatasetDict:
        dataset = dataset.remove_columns(["tokens"])

        xpos, deprels = self._get_labels(dataset, treebank_dataset)
        features = datasets.Features(
            {
                "input_ids": Sequence(feature=Value(dtype="int32", id=None), length=-1, id=None),
                "attention_mask": Sequence(feature=Value(dtype="int32", id=None), length=-1, id=None),
                "token_type_ids": Sequence(feature=Value(dtype="int32", id=None), length=-1, id=None),
                "token_spans": Sequence(feature=Value(dtype="int32", id=None), length=-1, id=None),
                "dependency_token_spans": Sequence(feature=Value(dtype="int32", id=None), length=-1, id=None),
                "head": Sequence(feature=Value(dtype="int16", id=None), length=-1, id=None),
                "deprel": Sequence(feature=ClassLabel(names=deprels, id=None), length=-1, id=None),
                "orig_head": Sequence(feature=Value(dtype="int16", id=None), length=-1, id=None),
                "orig_deprel": Sequence(feature=ClassLabel(names=deprels, id=None), length=-1, id=None),
                "xpos": Sequence(feature=ClassLabel(names=xpos, id=None), length=-1, id=None),
                "tree_is_gold": Sequence(feature=Value(dtype="int16", id=None), length=-1, id=None),
            }
        )

        new_dataset = {}
        for split, rows in dataset.items():

            def get_rows(rows, tree_is_gold=(0,)):
                def inner():
                    def process_row(v):
                        return {
                            "input_ids": v["input_ids"],
                            "token_type_ids": v["token_type_ids"],
                            "attention_mask": v["attention_mask"],
                            "token_spans": v["token_spans"],
                            "dependency_token_spans": v["dependency_token_spans"],
                            "head": [int(i) for i in v["head"]],
                            "deprel": v["deprel"],
                            "orig_head": [int(i) for i in v["orig_head"]],
                            "orig_deprel": v["orig_deprel"],
                            "xpos": v["xpos"],
                            "tree_is_gold": tree_is_gold,
                        }

                    for v in Tqdm.tqdm(rows, desc=f"Constructing {split}..", total=len(rows)):
                        yield process_row(v)

                return inner

            base_rows = get_rows(rows)
            treebank_rows = ()
            if treebank_dataset is not None:
                base_rows = list(base_rows())
                self.logger.info(f"Extending split {split} with gold treebanked sentences...")
                treebank_rows = list(get_rows(treebank_dataset[split], tree_is_gold=(1,))())
                if split == "train":
                    target_length = len(list(base_rows)) // unlabeled_per_labeled
                    if len(treebank_rows) > target_length:
                        raise Exception("More treebank rows than expected!")
                    self.logger.info(f"{len(treebank_rows)} treebank sequences found. Repeating...")
                    treebank_rows = list(itertools.islice(itertools.cycle(treebank_rows), target_length))
                    self.logger.info(
                        f"Upsampled treebank instances to {len(treebank_rows)}. (Unlabeled: {len(base_rows)})"
                    )
                treebank_rows = treebank_rows
            new_dataset[split] = Dataset.from_generator(
                lambda: chain(base_rows if isinstance(base_rows, list) else base_rows(), treebank_rows),
                features=features,
            )
            self.logger.info(f"Appended split {split} with {len(new_dataset[split])} sequences")

        return datasets.DatasetDict(new_dataset).with_format("torch")
