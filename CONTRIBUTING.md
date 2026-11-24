# Contributing to BSTK Chat (`bstkchat`)

Thank you for your interest in **BSTK Chat**! This project is **100% free, open-source (MIT License)**, and open to everyone—whether you want to fix a typo, improve the terminal/mobile UI, or add new decentralized features.

---

## Ways to Contribute
- **Report Bugs:** Open an issue on [GitHub Issues](https://github.com/BTKcreations/BSTKCHAT/issues) with steps to reproduce.
- **Suggest Features:** Share ideas for the CLI, desktop Web UI, or mobile PWA.
- **Submit Code or Docs:** Fork the repository, make your changes, and open a Pull Request.

---

## Local Development Setup

1. **Fork & clone the repository:**
   ```bash
   git clone https://github.com/BTKcreations/BSTKCHAT.git
   cd BSTKCHAT
   ```

2. **Install in editable mode with test tools:**
   ```bash
   python -m pip install -e . pytest
   ```

3. **Run the test suite:**
   ```bash
   python -m pytest -v
   ```

---

## Project Structure
- `bstkchat/cli.py` — Terminal UI, MQTT client, and slash commands (`--web`, `/web`, `/reply`, `/react`, `/delete`, etc.)
- `bstkchat/crypto.py` — `scrypt` key derivation and AES-256-GCM authenticated encryption
- `bstkchat/chain.py` — Decentralized SHA-256 hash-linked room storage and peer sync
- `bstkchat/config.py` — User preferences, recent rooms, file sanitization, and log search
- `bstkchat/web.py` & `bstkchat/web_ui.html` — Localhost HTTP + SSE server and desktop Web UI
- `docs/` — Mobile-first installable PWA hosted on GitHub Pages (`https://btkcreations.github.io/BSTKCHAT/`)
- `tests/` — Unit tests (`test_crypto.py`, `test_chain.py`, `test_cli.py`, `test_web.py`)

---

## Pull Request Guidelines
1. Keep changes focused and add/update tests in `tests/` when changing behavior.
2. Ensure all tests pass (`python -m pytest -v`) before opening a PR.
3. Never commit secrets, API tokens, or private keys.
4. Keep the browser/PWA UI (`docs/index.html` and `bstkchat/web_ui.html`) protocol-compatible with `bstkchat/cli.py`.
