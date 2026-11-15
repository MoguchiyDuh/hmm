import json
import sys

from hmm.app import create_app


def main() -> None:
    json.dump(create_app().openapi(), sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
