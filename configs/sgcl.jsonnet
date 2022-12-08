// --------------------------------------------------------------------------------
// Language settings
// --------------------------------------------------------------------------------
local language = std.extVar("LANGUAGE");
local language_code_index = {
    "coptic": "cop",
    "greek": "grc",
    "indonesian": "id",
    "maltese": "mt",
    "tamil": "ta",
    "uyghur": "ug",
    "wolof": "wo",
};
local stanza_do_not_retokenize = ["coptic"];
local stanza_no_mwt = ["greek", "uyghur", "maltese"];

// a helper
local stringifyPair(k,v) = std.toString(k) + "-" + std.toString(v);
local stringifyObject(o) = std.join('_', std.objectValues(std.mapWithKey(stringifyPair, o)));

// --------------------------------------------------------------------------------
// Model settings
// --------------------------------------------------------------------------------
local max_length = 512;


// For pretrained
// local FROM_PRETRAINED = true;
// local model_path = "distilbert-base-cased";
// local tokenizer = { pretrained_model_name_or_path: model_path };
// local model = {
//     type: "loreiba.sgcl.model::sgcl_model",
//     pretrained_model_name_or_path: model_path,
// };


// For non-pretrained
local FROM_PRETRAINED = false;
local roberta_config = {
    hidden_size: 128,
    num_hidden_layers: 3,
    num_attention_heads: 8,
    intermediate_size: 512,
    max_position_embeddings: max_length,
};
local model_path = "./workspace/models/" + language + "_" + stringifyObject(roberta_config);
local tokenizer = { pretrained_model_name_or_path: model_path };
local model = {
    type: "loreiba.sgcl.model::sgcl_model",
    roberta_config: roberta_config,
    tokenizer: tokenizer,
    model_output_path: model_path,
};

// --------------------------------------------------------------------------------
// Trainer settings
// --------------------------------------------------------------------------------
// BERT's original settings:
//    We train with batch size of 256 sequences
//    (256 sequences * 512 tokens = 128,000 tokens/batch)
//    for 1,000,000 steps, which is approximately 40
//    epochs over the 3.3 billion word corpus.
local BERT_batch_size = 256;
local BERT_steps = 1e6;
local BERT_total_instances = BERT_steps * BERT_batch_size;

// our settings
local batch_size = 64;
local instances_per_epoch = 256000;
local num_steps = BERT_steps * (BERT_batch_size / batch_size) / 16;  // 16 is an extra reduction we're making

local validate_every = 20000;

// --------------------------------------------------------------------------------
// Optimizer settings
// --------------------------------------------------------------------------------
local training_engine = {
    type: "torch",
    optimizer: {
        type: "torch::AdamW",
        lr: 3e-3,
        betas: [0.9, 0.99],
        eps: 1e-5,
        weight_decay: 0.05
    },
    lr_scheduler: {
        type: "transformers::cosine",
        num_warmup_steps: validate_every,
        num_training_steps: num_steps,
    },
    amp: false
};

local collate_fn = {
    type: "loreiba.sgcl.collator::collator",
    tokenizer: tokenizer,
};
local train_dataloader = {
    shuffle: true,
    batch_size: batch_size,
    collate_fn: collate_fn,
};
local val_dataloader = {
    shuffle: false,
    batch_size: batch_size,
    collate_fn: collate_fn
};

{
    steps: {
        raw_treebank_data: {
            type: "loreiba.data::read_ud_treebank",
            shortcut: language,
            tag: "r2.11"  // Use UD treebanks from release 2.11
        },
        bare_text_data: {
            type: "loreiba.data::read_text_only_conllu",
            shortcut: language,
            stanza_retokenize: if std.member(stanza_do_not_retokenize, language) then false else true,
            stanza_use_mwt: if std.member(stanza_no_mwt, language) then false else true,
            stanza_language_code: language_code_index[language],
        },
        [if FROM_PRETRAINED then null else "tokenizer"]: {
            type: "loreiba.data::train_tokenizer",
            dataset: { "type": "ref", "ref": "bare_text_data" },
            model_path: model_path
        },
        tokenized_text_data: {
            type: "loreiba.data::tokenize_plus",
            dataset: { "type": "ref", "ref": "bare_text_data" },
            max_length: max_length,
            tokenizer: tokenizer,
            step_extra_dependencies: if FROM_PRETRAINED then [] else [ {type: "ref", "ref": "tokenizer" } ]
        },
        parsed_text_data: {
            type: "loreiba.data::stanza_parse_dataset",
            dataset: { "type": "ref", "ref": "tokenized_text_data" },
            language_code: language_code_index[language],
            allow_retokenization: false,  // we tokenized earlier
            stanza_use_mwt: if std.member(stanza_no_mwt, language) then false else true,
            batch_size: 64,
        },
        model_inputs: {
            type: "loreiba.data::finalize",
            dataset: { "type": "ref", "ref": "parsed_text_data" },
        },
        trained_model: {
            type: "torch::train",
            model: model,
            dataset_dict: { type: "ref", ref: "model_inputs" },
            training_engine: training_engine,
            log_every: 1,
            train_dataloader: train_dataloader,
            //train_epochs: num_epochs,
            train_steps: num_steps,
            validate_every: validate_every,
            checkpoint_every: validate_every,
            validation_split: "dev",
            validation_dataloader: val_dataloader,
            val_metric_name: "loss",
            minimize_val_metric: true,
            callbacks: [
                {"type": "loreiba.model::write_model", path: model_path, model_attr: "encoder"}
            ],
        },
        //final_metrics: {
        //    type: "torch::eval",
        //    model: { type: "ref", ref: "trained_model" },
        //    dataset_dict: { type: "ref", ref: "stype_instances" },
        //    dataloader: val_dataloader,
        //    metric_names: ["loss", "accuracy"],
        //    test_split: "test",
        //},
    }
}
