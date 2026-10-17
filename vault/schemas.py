"""The IBM Jupyter Lab sample files the vault recognises (github.com/linuxone-community-cloud/jupyter-lab-ml).

The files themselves ship inside that Jupyter Lab environment on the LinuxONE VM, not in the GitHub repo. The required
columns below are the ones the repo's notebooks read in their own code, so a CSV is recognised by its header, not by
its name. A CSV with an unknown header is still sealed and hashed, as kind `other`, but nothing reads it. Non-CSV
files (the loan demo's PDFs) are sealed as kind `document`.
"""
from __future__ import annotations

KINDS = {
    "card_transactions": {
        "files": ("transactions_history.csv", "test_220_40k.csv"),
        "required": ("User", "Card", "Year", "Month", "Day", "Time", "Amount", "Use Chip", "Merchant Name",
                     "Merchant City", "Merchant State", "Zip", "MCC", "Errors?", "Is Fraud?"),
        "notebook": "Fraud_LSTM_Keras_TF.ipynb",
    },
    "clients": {
        "files": ("clients.csv",),
        "required": ("CONT_ID", "ACTIVITY_LEVEL", "AGE_YEARS", "ANNUAL_INCOME", "ANNUAL_INVEST", "CHURN"),
        "notebook": "Churn_Scikit_PyTorch.ipynb",
    },
    "client_transactions": {
        "files": ("transactions.csv",),
        "required": ("CONT_ID", "TX_TOTAL_AMT", "DATE_CREDIT"),
        "notebook": "Churn_Scikit_PyTorch.ipynb",
    },
    "loans": {
        "files": ("sample_loan.csv",),
        "required": (),  # the loan notebook reads rows by position only, so there is no header to check
        "notebook": "Loan_Approval_Demo.ipynb",
    },
}


def classify(rel_path: str, header: list) -> dict:
    """{"kind": ..., "expected": kind the file name suggests or None, "missing": columns it lacks for that kind}."""
    cols = set(header)
    for kind, spec in KINDS.items():
        if spec["required"] and set(spec["required"]) <= cols:
            return {"kind": kind, "expected": None, "missing": []}
    name = rel_path.rsplit("/", 1)[-1].lower()
    for kind, spec in KINDS.items():
        if name in spec["files"]:
            missing = [c for c in spec["required"] if c not in cols]
            return {"kind": "other" if missing else kind, "expected": kind, "missing": missing}
    return {"kind": "other", "expected": None, "missing": []}
