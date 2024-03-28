datasets = [
    "arxiv", "books2", "books3", "dm_math", "enron", "europarl", "free_law",
    "github", "hackernews", "nih", "opensubtitles", "owt2", "pg_19", "philpapers",
    "pile_cc", "pubmed_abs", "pubmed_central", "stack_exchange", "ubuntu_irc",
    "uspto", "wiki_en", "youtube_subtitles"
]

for dataset in datasets:
    yaml_content = f"""data:
  cache_dir: "gs://levanter-data/tokenized/{dataset}-llama2/"
  train_urls:
    - gs://levanter-data/pile-domains/{dataset}/{{00..29}}.jsonl.zst
  validation_urls:
    - gs://levanter-data/pile-domains/{dataset}/val.jsonl.zst
  tokenizer: "meta-llama/Llama-2-70b-hf"

model:
  type: llama
  # TODO: uncomment this once we resolve the resource exhaustion issue
hf_checkpoint: "meta-llama/Llama-2-7b-hf"
second_hf_checkpoint: "openlm-research/open_llama_7b"

trainer:
  wandb:
    project: "trace"
    name: "llama2_7b-openllama-{dataset}"
    tags: ["{dataset}"]
  mp: p=f32,c=bfloat16
  train_batch_size: 64 # set for v4-64 TPU
  num_train_steps: 2
  steps_per_eval: 1
  tensor_parallel_axes: ["mlp", "heads"]
  fsdp_axis: "embed"
  batch_axis: "batch"
  per_device_eval_parallelism: -1
  max_eval_batches: 1
"""

    yaml_filename = f"for_llama2_7b_{dataset}.yaml"
    with open(yaml_filename, "w") as file:
        file.write(yaml_content)