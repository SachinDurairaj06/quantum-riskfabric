# vault/ — the bank's sealed data store (owner: A)

The vault holds the bank's data at rest: IBM's Jupyter Lab sample files for LinuxONE, from [linuxone-community-cloud/jupyter-lab-ml](https://github.com/linuxone-community-cloud/jupyter-lab-ml). That repo has the notebooks only. The data files come preloaded in its Jupyter Lab environment on the LinuxONE VM, so copy them from there. The files live in `$RF_DATA/vault/` and never go into git.

## Use
```bash
# 1. copy the sample files from the Jupyter Lab environment into $RF_DATA/vault/ (keep sample_data/ as a subfolder)
python -m vault seal          # 2. hash and record every file in MANIFEST.json
python -m vault verify        # any time: exit 1 if any file changed, went missing or was added
python -m vault build         # 3. the bank's accounts, tiers and a replay for the lab
python -m bank load           # 4. balances and tiers into Valkey (reset does this before every run)
```
Add `--data DIR` to point at another data folder.

## What it recognises
Files are recognised by their header, using the columns the notebooks read in their own code:

| Kind | Usual file | Columns it needs | From the notebook |
|---|---|---|---|
| `card_transactions` | `transactions_history.csv` (and the notebook's `test_220_40k.csv`) | User, Card, Year, Month, Day, Time, Amount, Use Chip, Merchant Name, Merchant City, Merchant State, Zip, MCC, Errors?, Is Fraud? | Fraud_LSTM_Keras_TF |
| `clients` | `clients.csv` | CONT_ID, ACTIVITY_LEVEL, AGE_YEARS, ANNUAL_INCOME, ANNUAL_INVEST, CHURN | Churn_Scikit_PyTorch |
| `client_transactions` | `transactions.csv` | CONT_ID, TX_TOTAL_AMT, DATE_CREDIT | Churn_Scikit_PyTorch |
| `loans` | `sample_data/sample_loan.csv` | none (the notebook reads rows by position) | Loan_Approval_Demo |
| `document` | the loan demo's PDFs | sealed and hashed, never read | Loan_Approval_Demo |

A CSV with an unknown header is sealed as `other` and not read. If a known file name lacks a column, `seal` names the missing columns.

## What `build` makes
- **`bank/accounts.csv`:** one account per card (`card-<User>-<Card>`) and per client (`client-<CONT_ID>`), with the segment and opening balance rules of prd/03 section 4.2.
- **`bank/tiers.csv`:** risk tiers from each card's fraud history in the train and validation periods: top 2% `high`, next 8% `medium`, the rest `low`. This is the interim rule until A's model exists; A-08 adds the model's mean score.
- **`replay/ibm_cards_test.csv`:** the lab's replay (CONTRACTS section 10).
  - It holds the earliest test-period card payments, refunds left out, in time order.
  - Payees are `merchant-<Merchant Name>`. `label` is the file's `Is Fraud?`, which never reaches the system under test.
  - Use it with `--replay` until A's HI-Small replay exists.
- **`vault/BUILD.json`:** the sealed digest it was built from, the 60/20/20 time split, and the counts.

`build` refuses to run if `verify` fails, so every account traces back to a sealed digest.

## Tests
`python -m pytest vault`. The tests use SYNTHETIC look-alike files with the same headers (`vault/tests/lookalike.py`), never IBM data.
