# Claude Code 向け実行プロンプト
# 症例写真整理システム — Windows exe ビルド

---

## あなたへの依頼

このリポジトリを Windows 向け exe にビルドしてください。
以下の手順をすべて実行し、最後にビルドが成功したことを確認してください。

---

## 前提

- OS: Windows 11
- Python: 3.11 以上が必要（未インストールの場合は後述）
- 作業ディレクトリ: このファイルがあるフォルダ

---

## 手順

### Step 1: Python のインストール確認

```bash
python --version
```

3.11 以上が表示されれば OK。表示されない場合は以下を実行してインストール：

```bash
winget install Python.Python.3.11
```

インストール後、ターミナルを再起動してから続行。

---

### Step 2: uv のインストール

```bash
pip install uv
```

---

### Step 3: 依存ライブラリのインストール

```bash
uv sync
```

`Resolved` や `Audited` と表示されれば成功。

---

### Step 4: exe のビルド

```bash
uv run pyinstaller PhotoOrganizer.spec --clean
```

`dist\PhotoOrganizer.exe` が生成されれば成功。

---

### Step 5: 動作確認

以下のコマンドを実行して起動テストする：

```bash
dist\PhotoOrganizer.exe
```

画面に以下が表示されれば成功（config.json のパスエラーは正常、watch_folder が存在すれば Running になる）：

```
Photo Auto-Organization System
================================
```

---

### Step 6: 納品パッケージの作成

以下のファイルを `delivery` フォルダにまとめる：

```
delivery/
├── PhotoOrganizer.exe    ← dist\PhotoOrganizer.exe をコピー
├── config.json           ← 既存のものをコピー
├── qr-generator.html     ← 既存のものをコピー
├── qrcode.min.js         ← 既存のものをコピー
└── README_起動方法.txt   ← 既存のものをコピー
```

フォルダごと ZIP に圧縮して納品する。

---

## トラブルシューティング

### `Failed to execute script` エラーが出る場合

hiddenimports が足りていない可能性がある。以下を実行して詳細を確認：

```bash
uv run pyinstaller PhotoOrganizer.spec --clean --log-level DEBUG 2>&1 | findstr /i "error"
```

### `cv2` が見つからないエラーの場合

```bash
uv run python -c "import cv2; print(cv2.__version__)"
```

でインポートできることを確認してから再ビルド。

### Windows Defender がブロックする場合

`dist\PhotoOrganizer.exe` を右クリック → プロパティ → 「ブロックの解除」にチェック → OK

---

## 確認事項

ビルド完了後、以下をすべて確認してください：

- [ ] `dist\PhotoOrganizer.exe` が存在する
- [ ] ファイルサイズが 50MB 以上ある（小さすぎる場合はビルド失敗）
- [ ] ダブルクリックで黒い画面が開く
- [ ] `config.json` の `watch_folder` に実在するフォルダを指定して起動テスト
- [ ] 起動後に「Photo Auto-Organization System Running」と表示される

---

以上が完了したら、`delivery` フォルダを ZIP 圧縮して納品してください。
