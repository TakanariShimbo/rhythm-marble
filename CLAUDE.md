# CLAUDE.md

このファイルはClaude Codeがこのプロジェクトで作業する際のガイドラインです。

## 環境

- OS: WSL2 (Ubuntu on Windows)

## Node.js 環境

nvm で環境構築済み。グローバルコマンドが必要な場合は `npx` を使う。
**グローバルインストール禁止**: `npm install -g` や `npm i -g`
は使わない。グローバル環境を汚染しないよう、パッケージはプロジェクトローカルにインストールする。

```bash
# バージョン確認
node --version
npm --version

# パッケージインストール（ローカルのみ）
npm install        # 依存関係のインストール
npm install <pkg>  # パッケージ追加（-g は使わない）

# スクリプト実行
npm run <script>

# グローバルインストールの代わりに npx を使う
npx <command>
```

## Python 環境

uv で環境構築済み。 `pip` や `python` の直接呼び出しは避け、`uv`
を通じて実行する。 `pip install` や `pip install --user`
は使わない。グローバル環境を汚染しないよう、必ず `uv`
経由で仮想環境内にインストールする。仮想環境を有効化せず、 `uv run`
経由で実行する。初期化、仮想環境作成が未実施の場合、必要に応じて実施してよい。

```bash
# Pythonバージョン確認
uv python list

# プロジェクト初期化（pyproject.toml生成）
uv init

# 仮想環境作成
uv venv

# パッケージインストール
uv add <package>          # pyproject.tomlに追記して追加
uv add --dev <package>    # 開発依存として追加

# スクリプト実行（仮想環境を自動で使用）
uv run python script.py
uv run <command>

# 依存関係の同期
uv sync
```
