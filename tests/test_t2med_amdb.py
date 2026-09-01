import json
import sys
import tempfile
import unittest
from pathlib import Path

from kienzledoku_ocr_backfill.t2med_amdb import (
    T2medAmdbError,
    T2medAmdbResolver,
    read_service_config,
)


class T2medAmdbResolverTests(unittest.TestCase):
    def _environment(self, root: Path) -> tuple[Path, Path, Path, Path]:
        config = root / "service.conf"
        config.write_text(
            "dball.dbhost=localhost\n"
            "dball.dbport=16577\n"
            "dball.dbschema=mmidata1\n",
            encoding="utf-8",
        )
        socket = root / "t2med-mariadb"
        socket.touch()
        query_log = root / "queries.jsonl"
        client = root / "mariadb"
        client.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "query = sys.argv[sys.argv.index('--execute') + 1]\n"
            f"log = Path({str(query_log)!r})\n"
            "with log.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(query) + '\\n')\n"
            "if \"'sourceTable'\" in query:\n"
            "    print(json.dumps({'schema': 'mmidata1', "
            "'serverVersion': '11.4.5-MariaDB', "
            "'sourceTable': 'MEDPLAN_PACKAGE'}))\n"
            "elif \"'09322739'\" in query:\n"
            "    print(json.dumps({'pzn': '09322739', "
            "'name': 'Tamsulosin - 1 A Pharma 0,4 mg Retardtabletten', "
            "'substance': 'Tamsulosin', 'strength': '0,4 mg', "
            "'formIfa': 'RET', 'formMedicationPlan': 'RetTabl'}))\n",
            encoding="utf-8",
        )
        client.chmod(0o755)
        return config, client, socket, query_log

    def test_reads_active_schema_and_resolves_each_pzn_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, client, socket, query_log = self._environment(Path(tmp))
            progress: list[str] = []
            resolver = T2medAmdbResolver(
                config_path=config,
                client_path=client,
                socket_path=socket,
                progress=progress.append,
            )

            metadata = resolver.metadata()
            drug = resolver.lookup("PZN 9322739")
            missing = resolver.lookup("09531845")
            cached = resolver.lookup("09322739")

            self.assertEqual(metadata["schema"], "mmidata1")
            self.assertEqual(metadata["sourceTable"], "MEDPLAN_PACKAGE")
            self.assertEqual(drug["name"], "Tamsulosin - 1 A Pharma 0,4 mg Retardtabletten")
            self.assertEqual(drug["substances"], [{"name": "Tamsulosin", "strength": "0,4 mg"}])
            self.assertEqual(drug["form_long"], "RetTabl")
            self.assertIsNone(missing)
            self.assertIs(cached, drug)
            self.assertIn(
                "T2med-Arzneimitteldatenbank wird abgefragt: Schema mmidata1",
                progress,
            )
            self.assertTrue(any("PZN 09322739 wird abgefragt" in line for line in progress))
            self.assertTrue(any("Wirkstoff Tamsulosin" in line for line in progress))
            self.assertTrue(any("PZN 09531845 nicht gefunden" in line for line in progress))
            self.assertTrue(
                any(line.startswith("Dauer T2med-AMDB-Verbindung:") for line in progress)
            )
            self.assertTrue(
                any(
                    line.startswith("Dauer T2med-AMDB PZN 09322739:")
                    for line in progress
                )
            )

            queries = [
                json.loads(line)
                for line in query_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(queries), 3)
            for query in queries:
                self.assertIn("START TRANSACTION READ ONLY", query)
                self.assertTrue(query.rstrip().endswith("ROLLBACK;"))
                for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "REPLACE "):
                    self.assertNotIn(forbidden, query.upper())
            self.assertIn("FROM MEDPLAN_PACKAGE", queries[1])

    def test_rejects_invalid_schema_before_starting_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, client, socket, _ = self._environment(root)
            config.write_text("dball.dbschema=mmidata1; DROP DATABASE x\n", encoding="utf-8")
            with self.assertRaisesRegex(T2medAmdbError, "Ungültiges"):
                T2medAmdbResolver(
                    config_path=config,
                    client_path=client,
                    socket_path=socket,
                )

    def test_service_config_uses_last_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "service.conf"
            config.write_text(
                "# Wechsel während AMDB-Update\n"
                "dball.dbschema=mmidata1\n"
                "dball.dbschema=mmidata2\n",
                encoding="utf-8",
            )
            self.assertEqual(read_service_config(config)["dball.dbschema"], "mmidata2")


if __name__ == "__main__":
    unittest.main()
