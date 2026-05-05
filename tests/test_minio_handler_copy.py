import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ROOT_MINIO = ROOT / "minio_handler"
AIRFLOW_MINIO = ROOT / "airflow_mgmt" / "minio_handler"


class MinioHandlerCopyTests(unittest.TestCase):
    def test_airflow_copy_matches_root_package(self) -> None:
        for name in ["__init__.py", "base.py", "object.py"]:
            with self.subTest(name=name):
                self.assertEqual(
                    (AIRFLOW_MINIO / name).read_text(encoding="utf-8"),
                    (ROOT_MINIO / name).read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
