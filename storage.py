"""Персистентность базы данных для Hugging Face Spaces.

Диск контейнера Spaces эфемерный: после рестарта файл reminders.db пропал бы
вместе со всеми напоминаниями. Решение: копия БД хранится в приватном
HF Dataset, при старте подтягивается, изменения синхронизируются
(помечаются dirty при записи, сброс не чаще раза в 60 секунд).

Работает только когда заданы переменные окружения HF_DATASET_REPO и HF_TOKEN
(задаются как Secrets в настройках Space). Локально/на Render — no-op.
"""
import logging
import os
import time

log = logging.getLogger(__name__)

DB_PATH = "reminders.db"
FLUSH_INTERVAL = 60  # секунды между синхронизациями

_dataset = os.getenv("HF_DATASET_REPO")  # например "user/reminder-bot-db"
_token = os.getenv("HF_TOKEN")

_dirty = False
_last_flush = 0.0


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=_token)


def enabled() -> bool:
    return bool(_dataset and _token)


def init_remote_db() -> None:
    """Скачать reminders.db из датасета при старте (если копия есть)."""
    if not enabled():
        return
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download(repo_id=_dataset, filename=DB_PATH, repo_type="dataset",
                        token=_token, local_dir=".")
        log.info("БД загружена из датасета %s", _dataset)
    except Exception as exc:
        log.info("Копии БД в датасете нет, начинаем с чистой: %s", exc)


def mark_dirty() -> None:
    global _dirty
    _dirty = True


def maybe_flush(force: bool = False) -> None:
    """Залить БД в датасет, если были изменения (не чаще раза в 60 сек)."""
    global _dirty, _last_flush
    if not enabled():
        return
    now = time.time()
    if not force and (not _dirty or now - _last_flush < FLUSH_INTERVAL):
        return
    try:
        _api().upload_file(path_or_fileobj=DB_PATH, path_in_repo=DB_PATH,
                           repo_id=_dataset, repo_type="dataset",
                           commit_message="sync reminders.db")
        _dirty = False
        _last_flush = now
        log.info("БД синхронизирована с HF Dataset")
    except Exception:
        log.exception("Не удалось синхронизировать БД с HF Dataset")
