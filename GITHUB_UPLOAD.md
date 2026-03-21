# Uploading whale-scanner to GitHub

## 1. Create a new repository on GitHub

1. Go to [github.com/new](https://github.com/new)
2. Name it `whale-scanner` (or your preferred name)
3. Choose **Private** or **Public**
4. Do **not** initialize with a README, .gitignore, or license (this repo already has them)
5. Click **Create repository**

## 2. Initialize git and push from your machine

From your terminal, run:

```bash
cd /Users/dannyliu/Downloads/whale-scanner

# Initialize git (if not already)
git init

# Add all files
git add .

# First commit
git commit -m "Initial commit: whale-scanner skeleton per scanner_plan.md"

# Add your GitHub repo as remote (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/whale-scanner.git

# Push to main
git branch -M main
git push -u origin main
```

## 3. If you prefer SSH

```bash
git remote add origin git@github.com:YOUR_USERNAME/whale-scanner.git
git push -u origin main
```

## 4. Before pushing: ensure secrets are not committed

- `.env` is in `.gitignore` — never commit your `UW_API_TOKEN`
- Copy `.env.example` to `.env` locally and fill in your token; `.env` will not be pushed

## 5. Optional: add a GitHub Action for tests

Create `.github/workflows/test.yml`:

```yaml
name: Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v
```
