import os
import sys
import csv
import json
import time
import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import cv2
from PIL import Image
from PIL.ExifTags import TAGS
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


def _get_base_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _select_watch_folder() -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showinfo(
            "監視フォルダの設定",
            "次の画面で、写真を整理するフォルダを選択してください。"
        )
        folder = filedialog.askdirectory(
            title="監視フォルダを選択してください"
        )
        root.destroy()
        return folder if folder else None
    except Exception as e:
        print(f"フォルダ選択エラー: {e}")
        return None


def _select_wait_minutes() -> Optional[int]:
    """待機時間をメニューで選択する。"""
    try:
        import tkinter as tk
        from tkinter import simpledialog

        options = {
            "1": (1,  "1分（高速回線向け）"),
            "2": (2,  "2分（標準・推奨）"),
            "3": (5,  "5分（低速回線向け）"),
            "4": (10, "10分（確実重視）"),
        }

        print("\n待機時間を選択してください（QR検知後、同期完了を待つ時間）")
        print("-"*50)
        for key, (minutes, label) in options.items():
            print(f"  {key}. {label}")
        print("-"*50)

        while True:
            try:
                answer = input("選択 [1/2/3/4]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if answer in options:
                minutes, label = options[answer]
                print(f"\n✅ 待機時間を「{label}」に設定しました\n")
                return minutes
            print("1〜4で入力してください。")

    except Exception as e:
        print(f"待機時間選択エラー: {e}")
        return None


def _setup_config(config_path: Path) -> dict:
    default_config = {
        "watch_folder": "",
        "wait_minutes": 2,
        "log_file": "photo_processor.log",
        "log_level": "INFO",
        "supported_formats": [
            ".jpg", ".jpeg", ".png", ".gif", ".bmp",
            ".JPG", ".JPEG", ".PNG"
        ],
        "backup_folder_name": "_backup",
        "error_folder_name": "_error",
        "done_folder_name": "_done",
        "unprocessed_folder_name": "_unprocessed",
        "stop_on_error": False,
        "patient_stats_file": "patient_stats.json",
        "csv_log_file": "photo_history.csv"
    }

    config = default_config.copy()
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                config.update(loaded)
        except Exception:
            pass

    watch_folder = config.get("watch_folder", "")
    needs_setup = not watch_folder or not Path(watch_folder).exists()

    if needs_setup:
        print("\n" + "="*60)
        print("初期設定：監視フォルダを選択してください")
        print("="*60)
        selected = _select_watch_folder()
        if not selected:
            print("\nフォルダが選択されませんでした。終了します。")
            input("Enterキーを押すと終了します...")
            sys.exit(0)
        config["watch_folder"] = selected
        _save_config(config_path, config)
        print(f"\n✅ 監視フォルダを設定しました: {selected}\n")

    else:
        # 2回目以降：設定確認・変更メニュー
        wait_minutes = config.get("wait_minutes", 2)
        print("\n" + "="*60)
        print("現在の設定")
        print("="*60)
        print(f"  監視フォルダ : {watch_folder}")
        print(f"  待機時間    : {wait_minutes}分")
        print("="*60)
        print("設定を変更する場合は番号を入力してください。")
        print("  1. 監視フォルダを変更")
        print("  2. 待機時間を変更")
        print("  Enterキー: このまま起動")
        print("-"*60)

        try:
            answer = input("選択 [1/2/Enter]: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""

        if answer == "1":
            selected = _select_watch_folder()
            if selected:
                config["watch_folder"] = selected
                _save_config(config_path, config)
                print(f"\n✅ 監視フォルダを変更しました: {selected}\n")
            else:
                print("\nキャンセルしました。元の設定で起動します。\n")

        elif answer == "2":
            minutes = _select_wait_minutes()
            if minutes is not None:
                config["wait_minutes"] = minutes
                _save_config(config_path, config)

    return config


def _save_config(config_path: Path, config: dict):
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


class PhotoProcessor:

    def __init__(self, config_path: str = "config.json"):
        base_dir = _get_base_dir()
        resolved_config = base_dir / config_path
        self.config = _setup_config(resolved_config)

        self.watch_folder = Path(self.config['watch_folder'])
        self.wait_seconds = self.config.get('wait_minutes', 2) * 60
        self.backup_folder_name = self.config.get('backup_folder_name', '_backup')
        self.error_folder_name = self.config.get('error_folder_name', '_error')
        self.done_folder_name = self.config.get('done_folder_name', '_done')
        self.unprocessed_folder_name = self.config.get('unprocessed_folder_name', '_unprocessed')
        self.stop_on_error = self.config.get('stop_on_error', False)
        self.stop_requested = False
        self.patient_stats_file = self.config.get('patient_stats_file', 'patient_stats.json')
        self.csv_log_file = self.config.get('csv_log_file', 'photo_history.csv')

        formats = self.config.get('supported_formats', ['.jpg', '.jpeg', '.png', '.gif', '.bmp'])
        self._supported_formats = {fmt.lower() for fmt in formats}

        # 前回処理したQRファイル名を記録（これ以降のファイルを次のセッションの対象にする）
        self._last_processed_qr_name: Optional[str] = None

        self.setup_logging()

        if not self.watch_folder.exists():
            print(f"\n❌ 監視フォルダが見つかりません: {self.watch_folder}")
            print("   config.json を削除して再起動すると再設定できます。")
            input("Enterキーを押すと終了します...")
            raise FileNotFoundError(f"Watch folder not found: {self.watch_folder}")

    def setup_logging(self):
        log_file = self.config.get('log_file', 'photo_processor.log')
        log_level = getattr(logging, self.config.get('log_level', 'INFO').upper())
        log_path = _get_base_dir() / log_file

        self.logger = logging.getLogger('PhotoProcessor')
        self.logger.setLevel(log_level)
        if self.logger.handlers:
            self.logger.handlers.clear()
        self.logger.propagate = False

        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        console_handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        self.logger.info(f"監視フォルダ: {self.watch_folder}")
        self.logger.info(f"待機時間: {self.config.get('wait_minutes', 2)}分")

    def is_image_file(self, filepath: Path) -> bool:
        return filepath.suffix.lower() in self._supported_formats

    def _is_root_image(self, filepath: Path) -> bool:
        """監視フォルダのルート直下にある画像ファイルかどうかを確認する。
        サブフォルダ内のファイルは対象外。
        """
        return (
            filepath.parent.resolve() == self.watch_folder.resolve()
            and self.is_image_file(filepath)
        )

    def get_exif_date(self, image_path: Path) -> Optional[datetime]:
        try:
            image = Image.open(image_path)
            exif_data = image._getexif()
            if exif_data is None:
                return None
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, tag_id)
                if tag_name in ['DateTimeOriginal', 'DateTime']:
                    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
            return None
        except Exception:
            return None

    def get_image_timestamp(self, image_path: Path) -> datetime:
        exif_date = self.get_exif_date(image_path)
        if exif_date:
            return exif_date
        return datetime.fromtimestamp(image_path.stat().st_mtime)

    def detect_qr_code(self, image_path: Path) -> Optional[str]:
        """デジカメ高解像度写真対応QR検出。
        numpy経由で読み込み（日本語パス対応）。
        長辺600px・400pxに縮小して検出（高解像度対応）。
        """
        try:
            import numpy as np
            with open(image_path, 'rb') as f:
                data = np.frombuffer(f.read(), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                self.logger.warning(f"画像を読み込めませんでした: {image_path.name}")
                return None

            detector = cv2.QRCodeDetector()
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape

            for target_size in [600, 400]:
                scale = target_size / max(h, w)
                resized = cv2.resize(gray, None, fx=scale, fy=scale,
                                     interpolation=cv2.INTER_AREA)
                qr_data, _, _ = detector.detectAndDecode(resized)
                if qr_data:
                    self.logger.info(f"QR検出 ({target_size}px): {image_path.name} → {qr_data}")
                    return self.parse_patient_id(qr_data)

            # フォールバック: 元サイズ
            qr_data, _, _ = detector.detectAndDecode(gray)
            if qr_data:
                self.logger.info(f"QR検出 (元サイズ): {image_path.name} → {qr_data}")
                return self.parse_patient_id(qr_data)

            return None

        except Exception as e:
            self.logger.error(f"QR検出エラー {image_path.name}: {e}")
            return None

    def parse_patient_id(self, qr_data: str) -> Optional[str]:
        if qr_data.startswith("PATIENT_ID:"):
            return qr_data.replace("PATIENT_ID:", "").strip()
        return qr_data.strip()

    def _should_skip(self, path: Path) -> bool:
        """_や.で始まるファイル・フォルダをスキップする。"""
        return path.name.startswith('_') or path.name.startswith('.')

    def _wait_for_sync_complete(self, qr_path: Path):
        """Dropbox同期完了を待つ。
        ファイルサイズが安定するまで確認し、最低でもwait_seconds待つ。
        """
        wait_minutes = self.config.get('wait_minutes', 2)
        self.logger.info(
            f"QR検知: {qr_path.name} — {wait_minutes}分待機します（Dropbox同期完了待ち）"
        )
        print(f"\n📷 QRコードを検知しました: {qr_path.name}")
        print(f"⏳ Dropbox同期完了まで{wait_minutes}分待機します...")

        # 最低待機時間
        deadline = time.time() + self.wait_seconds

        # ファイルサイズ安定確認（10秒ごとにチェック）
        prev_sizes = {}
        stable_count = 0
        required_stable = 3  # 3回連続で変化なし = 安定とみなす

        while time.time() < deadline:
            time.sleep(10)
            current_sizes = {}
            for f in self.watch_folder.iterdir():
                if f.is_file() and not self._should_skip(f) and self.is_image_file(f):
                    try:
                        current_sizes[f.name] = f.stat().st_size
                    except Exception:
                        pass

            if current_sizes == prev_sizes and current_sizes:
                stable_count += 1
                if stable_count >= required_stable and time.time() >= deadline:
                    break
            else:
                stable_count = 0
            prev_sizes = current_sizes.copy()

        self.logger.info("待機完了。処理を開始します。")
        print("✅ 待機完了。写真を整理します...\n")

    def _get_files_since_last_qr(self) -> List[Path]:
        """監視フォルダのルート直下にある未処理ファイルを
        ファイル名順で返す。
        前回のQR以降のファイルが対象。
        """
        all_files = []
        for f in self.watch_folder.iterdir():
            if not f.is_file():
                continue
            if self._should_skip(f):
                continue
            if not self.is_image_file(f):
                continue
            all_files.append(f)

        # ファイル名順でソート
        all_files.sort(key=lambda f: f.name)

        # 前回のQR以降のファイルに絞る
        if self._last_processed_qr_name is None:
            return all_files

        result = []
        found = False
        for f in all_files:
            if found:
                result.append(f)
            if f.name == self._last_processed_qr_name:
                found = True

        # 前回QRが見つからない場合（すでに移動済み）は全ファイルを返す
        if not found:
            return all_files

        return result

    def _create_backup(self, session_id: str, photos: List[Path], qr_photo: Path, patient_id: str):
        backup_dir = self.watch_folder / self.backup_folder_name / session_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        for photo in photos:
            shutil.copy2(str(photo), str(backup_dir / photo.name))
        shutil.copy2(str(qr_photo), str(backup_dir / qr_photo.name))
        self.logger.info(f"バックアップ作成: {backup_dir} ({len(photos) + 1}件)")

    def _update_patient_stats(self, patient_id: str, photo_count: int):
        stats_path = _get_base_dir() / self.patient_stats_file
        stats = {}
        if stats_path.exists():
            try:
                with open(stats_path, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
            except Exception:
                pass
        stats[patient_id] = stats.get(patient_id, 0) + photo_count
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    def _append_csv_log(self, session_id: str, patient_id: str, photo_count: int, date_folder: str, status: str):
        csv_path = _get_base_dir() / self.csv_log_file
        write_header = not csv_path.exists()
        with open(csv_path, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(['timestamp', 'session_id', 'patient_id', 'photo_count', 'date_folder', 'status'])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session_id, patient_id, photo_count, date_folder, status,
            ])

    def _write_done(self, session_id: str, patient_id: str, count: int):
        done_dir = self.watch_folder / self.done_folder_name
        done_dir.mkdir(parents=True, exist_ok=True)
        with open(done_dir / f"done_{session_id}_{patient_id}.txt", "w", encoding="utf-8") as f:
            f.write(f"Patient: {patient_id}\nFiles moved: {count}\nCompleted: {datetime.now()}\n")

    def _write_error_report(self, session_id: str, patient_id: str, error: Exception, context: str):
        error_dir = self.watch_folder / self.error_folder_name
        error_dir.mkdir(parents=True, exist_ok=True)
        with open(error_dir / f"error_{session_id}.txt", 'w', encoding='utf-8') as f:
            f.write(
                f"Error Report\n============\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Session: {session_id}\nPatient: {patient_id}\n"
                f"Context: {context}\nError: {type(error).__name__}: {error}\n"
            )

    def organize_photos(self, patient_id: str, photos: List[Path], qr_photo: Path) -> int:
        """写真を患者番号/日付フォルダへ移動する。
        ファイル名はそのまま保持する。既存ファイルと名前が重複する場合のみ連番を付加。
        """
        qr_timestamp = self.get_image_timestamp(qr_photo)
        date_folder = qr_timestamp.strftime("%Y.%m.%d")
        dest_folder = self.watch_folder / patient_id / date_folder
        dest_folder.mkdir(parents=True, exist_ok=True)

        moved_count = 0

        for photo in photos:
            dest_path = dest_folder / photo.name
            # 同名ファイルが存在する場合は連番を付加
            if dest_path.exists():
                counter = 1
                while dest_path.exists():
                    dest_path = dest_folder / f"{photo.stem}_{counter}{photo.suffix}"
                    counter += 1
            shutil.move(str(photo), str(dest_path))
            moved_count += 1

        # QRファイルも移動（元のファイル名を保持）
        if qr_photo.exists():
            qr_dest = dest_folder / qr_photo.name
            if qr_dest.exists():
                counter = 1
                while qr_dest.exists():
                    qr_dest = dest_folder / f"{qr_photo.stem}_{counter}{qr_photo.suffix}"
                    counter += 1
            shutil.move(str(qr_photo), str(qr_dest))
            moved_count += 1
        else:
            self.logger.warning(f"QRファイルが見つかりません（スキップ）: {qr_photo.name}")

        return moved_count

    def _process_qr_trigger(self, qr_image_path: Path, patient_id: str):
        """QRトリガー処理。
        1. 同期完了まで待機
        2. 監視フォルダのルート直下ファイルをファイル名順で取得
        3. QRより前のファイルを対象写真とする（QR自身は含まない）
        4. バックアップ→移動→ログ
        """
        self._wait_for_sync_complete(qr_image_path)

        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.logger.info(f"処理開始: patient={patient_id}, session={session_id}")

        try:
            # 監視フォルダのルート直下のファイルをファイル名順で取得
            all_root_files = sorted(
                [f for f in self.watch_folder.iterdir()
                 if f.is_file()
                 and not self._should_skip(f)
                 and self.is_image_file(f)],
                key=lambda f: f.name
            )

            # QRより前のファイルのみを対象写真とする（QR自身は含まない）
            photos = []
            qr_found = False
            for f in all_root_files:
                if f.resolve() == qr_image_path.resolve():
                    qr_found = True
                    break
                photos.append(f)

            if not qr_found:
                self.logger.warning(f"QRファイルがファイル一覧に見つかりません: {qr_image_path.name}")

            self.logger.info(f"対象写真: {len(photos)}枚")
            for p in photos:
                self.logger.info(f"  → {p.name}")

            # バックアップ（QRが存在する場合のみQRもバックアップ）
            self._create_backup(session_id, photos, qr_image_path, patient_id)

            # 写真を移動（QRは含まない）
            qr_timestamp = self.get_image_timestamp(qr_image_path)
            date_folder = qr_timestamp.strftime("%Y.%m.%d")
            dest_folder = self.watch_folder / patient_id / date_folder
            dest_folder.mkdir(parents=True, exist_ok=True)

            moved_count = 0
            for photo in photos:
                if not photo.exists():
                    self.logger.warning(f"移動対象が見つかりません（スキップ）: {photo.name}")
                    continue
                dest_path = dest_folder / photo.name
                if dest_path.exists():
                    counter = 1
                    while dest_path.exists():
                        dest_path = dest_folder / f"{photo.stem}_{counter}{photo.suffix}"
                        counter += 1
                shutil.move(str(photo), str(dest_path))
                self.logger.info(f"移動: {photo.name} → {dest_path}")
                moved_count += 1

            # QRを移動（存在確認してから）
            if qr_image_path.exists():
                qr_dest = dest_folder / qr_image_path.name
                if qr_dest.exists():
                    counter = 1
                    while qr_dest.exists():
                        qr_dest = dest_folder / f"{qr_image_path.stem}_{counter}{qr_image_path.suffix}"
                        counter += 1
                shutil.move(str(qr_image_path), str(qr_dest))
                self.logger.info(f"QR移動: {qr_image_path.name} → {qr_dest}")
                moved_count += 1
            else:
                self.logger.warning(f"QRファイルが見つからないためスキップ: {qr_image_path.name}")

            # 前回QR名を更新
            self._last_processed_qr_name = qr_image_path.name

            self._write_done(session_id, patient_id, moved_count)
            self._update_patient_stats(patient_id, moved_count)
            self._append_csv_log(session_id, patient_id, moved_count,
                                  date_folder, "OK")

            self.logger.info(f"✅ 完了: patient={patient_id} {moved_count}枚移動")
            print(f"✅ 完了: 患者{patient_id} — {moved_count}枚を整理しました\n")

        except Exception as e:
            self.logger.error(f"処理エラー session={session_id}: {e}")
            self._write_error_report(session_id, patient_id, e, "処理中にエラー発生")
            self._append_csv_log(session_id, patient_id, 0, "", "ERROR")
            if self.stop_on_error:
                self.stop_requested = True

    def scan_existing_images(self):
        """起動時スキャン。
        ルート直下のファイルのみを対象とする。
        サブフォルダ（既存患者フォルダ等）は一切触らない。
        """
        self.logger.info("起動時スキャン開始（ルート直下のみ）...")
        root_files = []
        for f in self.watch_folder.iterdir():
            if not f.is_file():
                continue  # サブフォルダはスキップ
            if self._should_skip(f):
                continue
            if not self.is_image_file(f):
                continue
            root_files.append(f)

        if not root_files:
            self.logger.info("起動時スキャン: 対象ファイルなし")
            return

        self.logger.info(f"起動時スキャン: {len(root_files)}件のファイルを検出")

        # QRファイルがあれば処理する
        root_files.sort(key=lambda f: f.name)
        for f in root_files:
            patient_id = self.detect_qr_code(f)
            if patient_id:
                self.logger.info(f"起動時QR検出: {f.name} → patient={patient_id}")
                self._process_qr_trigger(f, patient_id)
                return  # 1セッション処理したら終了（残りは監視で処理）

        self.logger.info("起動時スキャン: QRファイルなし（監視待機に移行）")

    def run(self):
        self.scan_existing_images()

        event_handler = PhotoEventHandler(self)
        observer = Observer()
        observer.schedule(event_handler, str(self.watch_folder), recursive=False)
        observer.start()

        wait_minutes = self.config.get('wait_minutes', 2)
        print("\n" + "="*60)
        print("Photo Auto-Organization System Running")
        print("="*60)
        print(f"監視フォルダ : {self.watch_folder}")
        print(f"待機時間    : {wait_minutes}分")
        print("-"*60)
        print("診療中はこの画面を閉じないでください")
        print("停止するには Ctrl+C を押してください")
        print("="*60 + "\n")

        try:
            while not self.stop_requested:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        observer.stop()
        observer.join()
        self.logger.info("停止しました")


class PhotoEventHandler(FileSystemEventHandler):

    def __init__(self, processor: PhotoProcessor):
        self.processor = processor
        self._pending_qr: Optional[Path] = None

    def on_created(self, event):
        try:
            if event.is_directory:
                return
            file_path = Path(event.src_path)
            if not self.processor._is_root_image(file_path):
                return
            if self.processor._should_skip(file_path):
                return

            self.processor.logger.info(f"新しいファイル検知: {file_path.name}")

            # QRかどうか確認（書き込み完了を待ってから読む）
            time.sleep(3)
            if not file_path.exists():
                return

            patient_id = self.processor.detect_qr_code(file_path)
            if patient_id:
                self.processor.logger.info(f"QRトリガー: {file_path.name} → patient={patient_id}")
                self.processor._process_qr_trigger(file_path, patient_id)

        except Exception as e:
            self.processor.logger.error(f"ファイル処理中にエラーが発生しました: {e}")
            # エラーが発生してもプログラムは継続する


def main():
    print("Photo Auto-Organization System")
    print("================================\n")
    try:
        processor = PhotoProcessor()
        processor.run()
    except KeyboardInterrupt:
        print("\n停止しました。")
    except FileNotFoundError:
        sys.exit(1)
    except Exception as e:
        print(f"\n予期しないエラー: {e}")
        logging.exception("Fatal error")
        input("Enterキーを押すと終了します...")
        sys.exit(1)


if __name__ == "__main__":
    main()
