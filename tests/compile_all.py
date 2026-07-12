import py_compile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

TARGETS = [
    PROJECT_ROOT / "codes",
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "flash模式",
    PROJECT_ROOT / "专业模式",
]


def iter_python_files():
    for target in TARGETS:
        if target.exists():
            yield from sorted(target.glob("*.py"))


def main():
    files = list(iter_python_files())
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(f"compiled {len(files)} python files")


if __name__ == "__main__":
    main()
