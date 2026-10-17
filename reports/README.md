# Evaluation reports

Raw output of `scripts/evaluate.py`, kept so the numbers quoted in the project
README can be checked rather than taken on trust. Each file records the exact
command, model and date that produced it.

Regenerate with:

    python scripts/evaluate.py --set clean --runs 3 > reports/clean_runs3.txt
    python scripts/evaluate.py --set hard  --runs 3 > reports/hard_runs3.txt

Every run calls the API, so the output will differ in token counts even when
every extracted value is identical.
