
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATASET_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
)

# Tags documented by the official Bitext README.
KNOWN_FLAGS = set("BICNMLPQWKEZ")


def inspect_unknown_flags() -> None:
    """Identify flag characters not documented by Bitext."""

    df = pd.read_csv(DATASET_PATH)

    flag_values = df["flags"].dropna().astype(str)

    observed_flags = set(
        "".join(flag_values.tolist())
    )

    unknown_flags = sorted(
        observed_flags - KNOWN_FLAGS
    )

    print("\n=== UNKNOWN FLAG INVESTIGATION ===\n")

    print("Documented flag characters:")
    print(" ".join(sorted(KNOWN_FLAGS)))

    print("\nObserved flag characters:")
    print(" ".join(sorted(observed_flags)))

    print("\nUndocumented characters:")
    print(unknown_flags or "None")

    for flag in unknown_flags:
        matching_rows = df[
            df["flags"].astype(str).apply(
                lambda value: flag in value
            )
        ]

        print(f"\n--- Flag: {flag} ---")
        print(f"Number of affected records: {len(matching_rows)}")

        print(
            matching_rows[
                ["flags", "instruction", "category", "intent"]
            ].head(10).to_string(index=False)
        )

    print("\n=== INVESTIGATION COMPLETED ===")


if __name__ == "__main__":
    inspect_unknown_flags()
