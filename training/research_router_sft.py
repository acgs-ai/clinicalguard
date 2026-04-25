"""Starter SFT script for ClinicalGuard research-router local model training."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrainingDefaults:
    model_name: str = "unsloth/Qwen3.5-4B-Base"
    dataset_path: str = "training/router_dataset.jsonl"
    output_dir: str = "training/output-qwen35-router"
    max_seq_length: int = 2048
    learning_rate: float = 2e-4
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    num_train_epochs: int = 1
    lora_r: int = 16
    lora_alpha: int = 16
    lora_dropout: float = 0.0


def main() -> None:
    from datasets import load_dataset
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastLanguageModel

    defaults = TrainingDefaults()

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=defaults.model_name,
        max_seq_length=defaults.max_seq_length,
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r=defaults.lora_r,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        lora_alpha=defaults.lora_alpha,
        lora_dropout=defaults.lora_dropout,
        bias="none",
        use_gradient_checkpointing=True,
        random_state=3407,
    )

    dataset = load_dataset("json", data_files=defaults.dataset_path, split="train")

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            output_dir=defaults.output_dir,
            max_length=defaults.max_seq_length,
            learning_rate=defaults.learning_rate,
            per_device_train_batch_size=defaults.per_device_train_batch_size,
            gradient_accumulation_steps=defaults.gradient_accumulation_steps,
            num_train_epochs=defaults.num_train_epochs,
        ),
    )
    trainer.train()
    trainer.save_model(defaults.output_dir)


if __name__ == "__main__":
    main()
