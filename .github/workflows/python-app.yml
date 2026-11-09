name: XT Paper Trading Bot

on:
  workflow_dispatch:

  schedule:
    - cron: "*/15 * * * *"

  push:
    branches: [main]
    paths-ignore:
      - "paper_state.json"

permissions:
  contents: write

concurrency:
  group: xt-paper-trading
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: Get bot code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run paper trading bot
        run: python bot.py

      - name: Save virtual balance and trades
        run: |
          if [ -f paper_state.json ]; then
            git config user.name "github-actions[bot]"
            git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
            git add paper_state.json
            if ! git diff --cached --quiet; then
              git commit -m "Update paper trading state [skip ci]"
              git push
            fi
          fi
