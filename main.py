import os
import sys
import csv
import json
import time
import shutil
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

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
        folder = filedialog.askdirectory(title="監視フォルダを選択してください")
        root.destroy()
        return folder if folder else None
    except Exception as e:
        print(f"フォルダ選択エラー: {e}")
        return None


def _select_wait_minutes() -> Optional[int]:
    options = {
        "1": (1, "1分（高速回線向け）"),
        "2": (2, "2分（標準・推奨）"),
        "3": (5, "5分（低速回線向け）"),
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


def _save_config(config_path: Path, config: dict):
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


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
                config.update(json.load(f))
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

        # 処理の排他制御。待機タイマーと処理中フラグ。
        self._lock = threading.Lock()
        self._batch_timer: Optional[threading.Timer] = None
        self._processing = False

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

    def _should_skip(self, path: Path) -> bool:
        return path.name.startswith('_') or path.name.startswith('.')

    def _is_root_image(self, filepath: Path) -> bool:
        try:
            return (
                filepath.parent.resolve() == self.watch_folder.resolve()
                and self.is_image_file(filepath)
                and not self._should_skip(filepath)
            )
        except Exception:
            return False

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
        numpy経由で読み込み（日本語パス対応）、
        長辺600px・400pxに縮小して検出（高解像度対応）。
        """
        try:
            import numpy as np
            with open(image_path, 'rb') as f:
                data = np.frombuffer(f.read(), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
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
                    return self.parse_patient_id(qr_data)

            qr_data, _, _ = detector.detectAndDecode(gray)
            if qr_data:
                return self.parse_patient_id(qr_data)

            return None
        except Exception as e:
            self.logger.error(f"QR検出エラー {image_path.name}: {e}")
            return None

    def parse_patient_id(self, qr_data: str) -> Optional[str]:
        if qr_data.startswith("PATIENT_ID:"):
            return qr_data.replace("PATIENT_ID:", "").strip()
        return qr_data.strip()

    # ------------------------------------------------------------------
    # バッチ処理（複数QR対応の中核）
    # ------------------------------------------------------------------

    def schedule_batch_processing(self):
        """ファイル検知時に呼ばれる。
        待機タイマーをセット（既にあればリセット）。
        待機完了後に process_batch を実行する。
        新しいファイルが来るたびにタイマーをリセットするので、
        全ファイルの追加が止まってから待機時間後に処理が走る。
        """
        with self._lock:
            if self._processing:
                return  # 処理中は新しいタイマーをセットしない
            if self._batch_timer is not None:
                self._batch_timer.cancel()
            self._batch_timer = threading.Timer(self.wait_seconds, self.process_batch)
            self._batch_timer.daemon = True
            self._batch_timer.start()
            wait_minutes = self.config.get('wait_minutes', 2)
            self.logger.info(f"ファイル追加を検知。{wait_minutes}分後に整理を実行します（追加が続く間は延長）。")
            print(f"\n📷 写真を検知しました。{wait_minutes}分後に整理します...")

    def process_batch(self):
        """待機完了後に呼ばれる。
        監視フォルダのルート直下の全ファイルをファイル名順に並べ、
        QRを区切りとして各患者ごとに分割処理する。
        """
        with self._lock:
            if self._processing:
                return
            self._processing = True

        try:
            self._do_process_batch()
        except Exception as e:
            self.logger.error(f"バッチ処理中にエラー: {e}")
        finally:
            with self._lock:
                self._processing = False

            # 処理後にまだルート直下にファイルが残っていれば再スケジュール
            # （QRなしの写真だけ残っているケース等）
            remaining = self._get_root_images()
            if remaining and not self.stop_requested:
                self.logger.info(f"未処理ファイルが{len(remaining)}件残っています（QR待ち）。")

    def _get_root_images(self) -> List[Path]:
        """監視フォルダのルート直下の画像ファイルをファイル名順で返す。"""
        files = []
        for f in self.watch_folder.iterdir():
            if not f.is_file():
                continue
            if self._should_skip(f):
                continue
            if not self.is_image_file(f):
                continue
            files.append(f)
        files.sort(key=lambda f: f.name)
        return files

    def _do_process_batch(self):
        """ルート直下の全ファイルをファイル名順に並べ、
        QRごとに区切って各患者を処理する。
        """
        all_files = self._get_root_images()
        if not all_files:
            self.logger.info("処理対象のファイルがありません。")
            return

        self.logger.info("="*50)
        self.logger.info(f"整理処理を開始します（対象 {len(all_files)} 件）")

        # 各ファイルがQRかどうかを判定（patient_idを取得）
        # (file_path, patient_id or None) のリストを作る
        file_qr_list: List[Tuple[Path, Optional[str]]] = []
        for f in all_files:
            patient_id = self.detect_qr_code(f)
            file_qr_list.append((f, patient_id))

        # QRを区切りとして患者ごとに分割
        # 「あるQRより前で、かつ前のQRより後の写真」がそのQRの患者の写真
        current_group: List[Path] = []
        processed_any = False
        leftover_no_qr: List[Path] = []

        for f, patient_id in file_qr_list:
            if patient_id is not None:
                # QR発見 → current_groupがこの患者の写真
                self._process_one_patient(patient_id, current_group, f)
                current_group = []
                processed_any = True
            else:
                current_group.append(f)

        # 最後のQR以降に残った写真（QRがまだ来ていない患者の写真）
        leftover_no_qr = current_group

        if leftover_no_qr:
            self.logger.info(
                f"QR未検出の写真が{len(leftover_no_qr)}件あります。次のQR待ちのため保持します。"
            )

        if not processed_any:
            self.logger.info("QRコードが見つかりませんでした。次のQR待ちです。")

        self.logger.info("整理処理が完了しました。")
        self.logger.info("="*50)

    def _process_one_patient(self, patient_id: str, photos: List[Path], qr_photo: Path):
        """1人の患者分の写真とQRを整理する。"""
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + patient_id
        self.logger.info(f"▼ 患者 {patient_id} を処理（写真 {len(photos)} 枚 + QR）")

        try:
            qr_timestamp = self.get_image_timestamp(qr_photo)
            date_folder = qr_timestamp.strftime("%Y.%m.%d")
            dest_folder = self.watch_folder / patient_id / date_folder
            dest_folder.mkdir(parents=True, exist_ok=True)

            # バックアップ作成
            self._create_backup(session_id, photos, qr_photo)

            moved_count = 0

            # 写真を移動（元のファイル名を保持）
            for photo in photos:
                if not photo.exists():
                    self.logger.warning(f"移動対象が見つかりません（スキップ）: {photo.name}")
                    continue
                dest_path = self._unique_dest(dest_folder, photo.name)
                shutil.move(str(photo), str(dest_path))
                self.logger.info(f"  移動: {photo.name}")
                moved_count += 1

            # QRを移動
            if qr_photo.exists():
                qr_dest = self._unique_dest(dest_folder, qr_photo.name)
                shutil.move(str(qr_photo), str(qr_dest))
                self.logger.info(f"  QR移動: {qr_photo.name}")
                moved_count += 1
            else:
                self.logger.warning(f"QRファイルが見つかりません（スキップ）: {qr_photo.name}")

            self._write_done(session_id, patient_id, moved_count)
            self._update_patient_stats(patient_id, moved_count)
            self._append_csv_log(session_id, patient_id, moved_count, date_folder, "OK")

            self.logger.info(f"✅ 患者 {patient_id} 完了：{moved_count}件を {patient_id}/{date_folder} へ移動")
            print(f"✅ 患者 {patient_id}：{moved_count}件を整理しました")

        except Exception as e:
            self.logger.error(f"患者 {patient_id} の処理でエラー: {e}")
            self._write_error_report(session_id, patient_id, e, "患者処理中のエラー")
            self._append_csv_log(session_id, patient_id, 0, "", "ERROR")

    def _unique_dest(self, dest_folder: Path, filename: str) -> Path:
        """移動先で同名ファイルがある場合は連番を付加してユニークにする。"""
        dest_path = dest_folder / filename
        if not dest_path.exists():
            return dest_path
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        counter = 1
        while dest_path.exists():
            dest_path = dest_folder / f"{stem}_{counter}{suffix}"
            counter += 1
        return dest_path

    def _create_backup(self, session_id: str, photos: List[Path], qr_photo: Path):
        backup_dir = self.watch_folder / self.backup_folder_name / session_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for photo in photos:
            if photo.exists():
                shutil.copy2(str(photo), str(backup_dir / photo.name))
                count += 1
        if qr_photo.exists():
            shutil.copy2(str(qr_photo), str(backup_dir / qr_photo.name))
            count += 1
        self.logger.info(f"  バックアップ作成: {count}件")

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

    def _append_csv_log(self, session_id, patient_id, photo_count, date_folder, status):
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

    def _write_done(self, session_id, patient_id, count):
        done_dir = self.watch_folder / self.done_folder_name
        done_dir.mkdir(parents=True, exist_ok=True)
        with open(done_dir / f"done_{session_id}.txt", "w", encoding="utf-8") as f:
            f.write(f"Patient: {patient_id}\nFiles moved: {count}\nCompleted: {datetime.now()}\n")

    def _write_error_report(self, session_id, patient_id, error, context):
        error_dir = self.watch_folder / self.error_folder_name
        error_dir.mkdir(parents=True, exist_ok=True)
        with open(error_dir / f"error_{session_id}.txt", 'w', encoding='utf-8') as f:
            f.write(
                f"Error Report\n============\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Session: {session_id}\nPatient: {patient_id}\n"
                f"Context: {context}\nError: {type(error).__name__}: {error}\n"
            )

    def scan_existing_images(self):
        """起動時スキャン。ルート直下にファイルがあればバッチ処理をスケジュール。"""
        self.logger.info("起動時スキャン（ルート直下のみ）...")
        root_files = self._get_root_images()
        if root_files:
            self.logger.info(f"起動時：{len(root_files)}件のファイルを検出。整理をスケジュールします。")
            self.schedule_batch_processing()
        else:
            self.logger.info("起動時：対象ファイルなし。")

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

        if self._batch_timer is not None:
            self._batch_timer.cancel()
        observer.stop()
        observer.join()
        self.logger.info("停止しました")


class PhotoEventHandler(FileSystemEventHandler):

    def __init__(self, processor: PhotoProcessor):
        self.processor = processor

    def on_created(self, event):
        try:
            if event.is_directory:
                return
            file_path = Path(event.src_path)
            if not self.processor._is_root_image(file_path):
                return
            self.processor.logger.info(f"新しいファイル検知: {file_path.name}")
            self.processor.schedule_batch_processing()
        except Exception as e:
            self.processor.logger.error(f"ファイル検知処理でエラー: {e}")


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
