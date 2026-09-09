# RiskRadar — ML / Data Engineering

Real-Time Transaction Fraud Detection System for Nigerian financial services.
This folder contains the ML / Data Engineering work: dataset exploration, feature
engineering, the transaction simulator, and the baseline fraud detection model.

## Structure

```
notebooks/    Jupyter notebooks (EDA, feature engineering, model training)
data/         raw/ and processed/ datasets (not committed — see .gitignore)
models/       trained model artifacts (not committed — see .gitignore)
simulator/    synthetic transaction generator for demo scenarios
src/          reusable feature-engineering / helper code
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
