import os
import sys
import csv
import json
import time
import shutil
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import cv2
from PIL import Image
from PIL.ExifTags import TAGS
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


def _get_base_dir() -> Path:
    """exeでもスクリプトでも、実行ファイルと同じディレクトリを返す。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _select_watch_folder() -> Optional[str]:
    """tkinterのフォルダ選択ダイアログを開く。キャンセル時はNoneを返す。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox

        root = tk.Tk()
        root.withdraw()  # メインウィンドウを非表示
        root.attributes('-topmost', True)  # 最前面に表示

        messagebox.showinfo(
            "初期設定",
            "次の画面で、写真を入れるフォルダ（Dropboxフォルダ）を選択してください。"
        )

        folder = filedialog.askdirectory(
            title="監視フォルダを選択してください（写真を入れるDropboxフォルダ）"
        )
        root.destroy()

        return folder if folder else None

    except Exception as e:
        print(f"フォルダ選択ダイアログのエラー: {e}")
        return None


def _setup_config(config_path: Path) -> dict:
    """
    config.jsonが存在しない、またはwatch_folderが未設定の場合に
    フォルダ選択ダイアログを表示して設定を作成する。
    """
    # デフォルト設定
    default_config = {
        "watch_folder": "",
        "log_file": "photo_processor.log",
        "log_level": "INFO",
        "supported_formats": [
            ".jpg", ".jpeg", ".png", ".gif", ".bmp",
            ".JPG", ".JPEG", ".PNG"
        ],
        "max_photos_per_session": 200,
        "max_minutes_window": 60,
        "startup_scan_minutes": 30,
        "backup_folder_name": "_backup",
        "error_folder_name": "_error",
        "done_folder_name": "_done",
        "unprocessed_folder_name": "_unprocessed",
        "stop_on_error": True,
        "patient_stats_file": "patient_stats.json",
        "csv_log_file": "photo_history.csv"
    }

    # 既存のconfig.jsonを読み込む
    config = default_config.copy()
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                config.update(loaded)
        except Exception:
            pass

    # watch_folderが未設定または存在しないフォルダの場合はダイアログを表示
    watch_folder = config.get("watch_folder", "")
    needs_setup = (
        not watch_folder or
        not Path(watch_folder).exists()
    )

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

        # config.jsonに保存
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)

        print(f"\n✅ 設定を保存しました")
        print(f"   監視フォルダ: {selected}")

    return config


class PhotoProcessor:

    def __init__(self, config_path: str = "config.json"):
        base_dir = _get_base_dir()
        resolved_config = base_dir / config_path

        # 設定ファイルの初期化（必要であればフォルダ選択ダイアログを表示）
        self.config = _setup_config(resolved_config)

        self.watch_folder = Path(self.config['watch_folder'])
        self.max_photos_per_session = self.config.get('max_photos_per_session', 200)
        self.max_minutes_window = self.config.get('max_minutes_window', 60)
        self.backup_folder_name = self.config.get('backup_folder_name', '_backup')
        self.error_folder_name = self.config.get('error_folder_name', '_error')
        self.done_folder_name = self.config.get('done_folder_name', '_done')
        self.unprocessed_folder_name = self.config.get('unprocessed_folder_name', '_unprocessed')
        self.startup_scan_minutes = self.config.get('startup_scan_minutes', 30)
        self.stop_on_error = self.config.get('stop_on_error', False)
        self.stop_requested = False
        self.patient_stats_file = self.config.get('patient_stats_file', 'patient_stats.json')
        self.csv_log_file = self.config.get('csv_log_file', 'photo_history.csv')

        formats = self.config.get('supported_formats', ['.jpg', '.jpeg', '.png', '.gif', '.bmp'])
        self._supported_formats = {fmt.lower() for fmt in formats}

        self.setup_logging()

        if not self.watch_folder.exists():
            self.logger.error(f"Watch folder does not exist: {self.watch_folder}")
            print(f"\n❌ 監視フォルダが見つかりません: {self.watch_folder}")
            print("   config.json を削除して再起動すると、フォルダを再設定できます。")
            input("Enterキーを押すと終了します...")
            raise FileNotFoundError(f"Watch folder not found: {self.watch_folder}")

        self.logger.info("Photo Processor initialized")
        self.logger.info(f"Watching folder: {self.watch_folder}")

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
        file_handler.setLevel(log_level)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)

        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def is_image_file(self, filepath: Path) -> bool:
        return filepath.suffix.lower() in self._supported_formats

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
        except Exception as e:
            self.logger.warning(f"Could not extract EXIF from {image_path.name}: {e}")
            return None

    def get_image_timestamp(self, image_path: Path) -> datetime:
        exif_date = self.get_exif_date(image_path)
        if exif_date:
            return exif_date
        self.logger.debug(f"Using file modification time for {image_path.name}")
        return datetime.fromtimestamp(image_path.stat().st_mtime)

    def detect_qr_code(self, image_path: Path) -> Optional[str]:
        """WeChatQRCode（高精度）+ QRCodeDetector（フォールバック）"""
        try:
            image = cv2.imread(str(image_path))
            if image is None:
                self.logger.warning(f"Could not read image: {image_path.name}")
                return None

            # 第1試行: WeChatQRCode（遠距離・斜め撮影に強い）
            try:
                wechat = cv2.wechat_qrcode_WeChatQRCode()
                results, _ = wechat.detectAndDecode(image)
                if results:
                    self.logger.info(f"QR detected (WeChatQRCode) in {image_path.name}: {results[0]}")
                    return self.parse_patient_id(results[0])
            except Exception:
                pass

            # 第2試行: グレースケール
            detector = cv2.QRCodeDetector()
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            qr_data, _, _ = detector.detectAndDecode(gray)
            if qr_data:
                self.logger.info(f"QR detected (gray) in {image_path.name}: {qr_data}")
                return self.parse_patient_id(qr_data)

            # 第3試行: 拡大
            height, width = image.shape[:2]
            if max(height, width) < 2000:
                enlarged = cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                qr_data, _, _ = detector.detectAndDecode(enlarged)
                if qr_data:
                    self.logger.info(f"QR detected (enlarged) in {image_path.name}: {qr_data}")
                    return self.parse_patient_id(qr_data)

            return None

        except Exception as e:
            self.logger.error(f"Error detecting QR code in {image_path.name}: {e}")
            return None

    def parse_patient_id(self, qr_data: str) -> Optional[str]:
        if qr_data.startswith("PATIENT_ID:"):
            return qr_data.replace("PATIENT_ID:", "").strip()
        return qr_data.strip()

    def _should_skip_path(self, path: Path) -> bool:
        name = path.name
        return name.startswith('_') or name.startswith('.')

    def _collect_qualifying_photos(self, qr_timestamp: datetime, qr_image_path: Path) -> List[Path]:
        cutoff_time = qr_timestamp - timedelta(minutes=self.max_minutes_window)
        qr_resolved = qr_image_path.resolve()
        qualifying = []

        for file in self.watch_folder.iterdir():
            if not file.is_file():
                continue
            if self._should_skip_path(file):
                continue
            if not self.is_image_file(file):
                continue
            if file.resolve() == qr_resolved:
                continue
            timestamp = self.get_image_timestamp(file)
            if cutoff_time <= timestamp <= qr_timestamp:
                qualifying.append((timestamp, file))

        qualifying.sort(key=lambda item: item[0])
        return [file for _, file in qualifying]

    def _generate_session_id(self, qr_timestamp: datetime) -> str:
        return qr_timestamp.strftime("%Y%m%d_%H%M%S")

    def _create_backup(self, session_id: str, photos: List[Path], qr_photo: Path, patient_id: str) -> Path:
        backup_dir = self.watch_folder / self.backup_folder_name / session_id
        backup_dir.mkdir(parents=True, exist_ok=True)

        for i, photo in enumerate(photos):
            new_name = f"{i + 1:03d}{photo.suffix}"
            shutil.copy2(str(photo), str(backup_dir / new_name))

        qr_backup_name = f"QR_{patient_id}{qr_photo.suffix}"
        shutil.copy2(str(qr_photo), str(backup_dir / qr_backup_name))

        self.logger.info(f"Backup created: {backup_dir} ({len(photos) + 1} files)")
        return backup_dir

    def _move_to_unprocessed(self, image_path: Path):
        unprocessed_dir = self.watch_folder / self.unprocessed_folder_name
        unprocessed_dir.mkdir(parents=True, exist_ok=True)
        dest = unprocessed_dir / image_path.name
        if dest.exists():
            counter = 1
            while dest.exists():
                dest = unprocessed_dir / f"{image_path.stem}_{counter}{image_path.suffix}"
                counter += 1
        shutil.move(str(image_path), str(dest))
        self.logger.info(f"Moved to unprocessed: {image_path.name} -> {dest.name}")

    def _update_patient_stats(self, patient_id: str, photo_count: int):
        stats_path = _get_base_dir() / self.patient_stats_file
        stats = {}
        if stats_path.exists():
            try:
                with open(stats_path, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
            except Exception:
                stats = {}
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
        error_file = error_dir / f"error_{session_id}.txt"
        with open(error_file, 'w', encoding='utf-8') as f:
            f.write(
                f"Error Report\n============\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Session ID: {session_id}\n"
                f"Patient ID: {patient_id}\n"
                f"Context: {context}\n"
                f"Error Type: {type(error).__name__}\n"
                f"Error Message: {str(error)}\n"
            )
        self.logger.error(f"Error report written to {error_file}")

    def organize_photos(self, patient_id: str, photos: List[Path], qr_photo: Path, qr_timestamp: datetime) -> int:
        date_folder = qr_timestamp.strftime("%Y.%m.%d")
        dest_folder = self.watch_folder / patient_id / date_folder
        dest_folder.mkdir(parents=True, exist_ok=True)

        existing_nums = [int(f.stem) for f in dest_folder.iterdir() if f.is_file() and f.stem.isdigit()]
        seq_start = max(existing_nums, default=0) + 1

        moved_count = 0
        for i, image_path in enumerate(photos):
            new_name = f"{seq_start + i:03d}{image_path.suffix}"
            shutil.move(str(image_path), str(dest_folder / new_name))
            moved_count += 1

        qr_dest_name = f"QR_{patient_id}{qr_photo.suffix}"
        qr_dest_path = dest_folder / qr_dest_name
        if qr_dest_path.exists():
            counter = 1
            while qr_dest_path.exists():
                qr_dest_path = dest_folder / f"QR_{patient_id}_{counter}{qr_photo.suffix}"
                counter += 1
        shutil.move(str(qr_photo), str(qr_dest_path))
        moved_count += 1

        return moved_count

    def _process_qr_trigger(self, qr_image_path: Path, patient_id: str):
        qr_timestamp = self.get_image_timestamp(qr_image_path)
        session_id = self._generate_session_id(qr_timestamp)
        self.logger.info(f"QR trigger: patient={patient_id}, session={session_id}")

        try:
            qualifying_photos = self._collect_qualifying_photos(qr_timestamp, qr_image_path)

            if len(qualifying_photos) > self.max_photos_per_session:
                error_msg = f"Photo count {len(qualifying_photos)} exceeds maximum {self.max_photos_per_session}"
                self.logger.error(error_msg)
                self._write_error_report(session_id, patient_id, ValueError(error_msg), "Max photos exceeded")
                self._append_csv_log(session_id, patient_id, len(qualifying_photos), "", "ERROR_MAX_EXCEEDED")
                return

            self._create_backup(session_id, qualifying_photos, qr_image_path, patient_id)
            moved_count = self.organize_photos(patient_id, qualifying_photos, qr_image_path, qr_timestamp)
            self._write_done(session_id, patient_id, moved_count)
            date_folder = qr_timestamp.strftime("%Y.%m.%d")
            self._update_patient_stats(patient_id, moved_count)
            self._append_csv_log(session_id, patient_id, moved_count, date_folder, "OK")
            self.logger.info(f"OK patient={patient_id} count={moved_count} session={session_id}")

        except Exception as e:
            self.logger.error(f"Session {session_id} failed: {e}")
            self._write_error_report(session_id, patient_id, e, "Session processing failed")
            self._append_csv_log(session_id, patient_id, 0, "", "ERROR")
            if self.stop_on_error:
                self.stop_requested = True

    def process_images(self, new_images: List[Path], move_unprocessed: bool = False):
        for image_path in new_images:
            if not image_path.exists():
                continue
            patient_id = self.detect_qr_code(image_path)
            if patient_id:
                self._process_qr_trigger(image_path, patient_id)
            elif move_unprocessed:
                self._move_to_unprocessed(image_path)

    def scan_existing_images(self):
        self.logger.info("Scanning for existing images...")
        cutoff = datetime.now() - timedelta(minutes=self.startup_scan_minutes)
        expired = datetime.now() - timedelta(minutes=self.max_minutes_window)

        recent, stale = [], []
        for file in self.watch_folder.iterdir():
            if not file.is_file() or self._should_skip_path(file) or not self.is_image_file(file):
                continue
            ts = self.get_image_timestamp(file)
            if ts >= cutoff:
                recent.append(file)
            elif ts < expired:
                stale.append(file)

        if recent:
            self.logger.info(f"Found {len(recent)} recent images")
            self.process_images(recent)
        if stale:
            self.logger.info(f"Found {len(stale)} stale images, moving to unprocessed")
            self.process_images(stale, move_unprocessed=True)

    def run(self):
        self.logger.info("Starting Photo Processor...")
        self.scan_existing_images()

        event_handler = PhotoEventHandler(self)
        observer = Observer()
        observer.schedule(event_handler, str(self.watch_folder), recursive=False)
        observer.start()

        print("\n" + "="*60)
        print("Photo Auto-Organization System Running")
        print("="*60)
        print(f"監視フォルダ : {self.watch_folder}")
        print("-"*60)
        print("診療中はこの画面を閉じないでください")
        print("停止するには Ctrl+C を押してください")
        print("="*60 + "\n")

        try:
            while not self.stop_requested:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        self.logger.info("Stopping Photo Processor...")
        observer.stop()
        observer.join()
        self.logger.info("Photo Processor stopped")


class PhotoEventHandler(FileSystemEventHandler):

    def __init__(self, processor: PhotoProcessor):
        self.processor = processor
        self.process_delay = 2

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        if file_path.name.startswith('_') or file_path.name.startswith('.'):
            return
        if self.processor.is_image_file(file_path):
            self.processor.logger.info(f"New image detected: {file_path.name}")
            time.sleep(self.process_delay)
            self.processor.process_images([file_path])


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
        print(f"\n予期しないエラーが発生しました: {e}")
        logging.exception("Fatal error occurred")
        input("Enterキーを押すと終了します...")
        sys.exit(1)


if __name__ == "__main__":
    main()
