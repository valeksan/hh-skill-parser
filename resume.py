"""Продолжает незавершённый сбор, сохраняя региональный progress и CSV."""

from start import run_collection


if __name__ == "__main__":
    run_collection(resume=True)
