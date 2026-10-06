from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "synthetic"


def sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()
