import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from modeling import LogisticModel, features, samples, score
from storage import connect, insert_rounds, load_rounds


class PipelineTests(unittest.TestCase):
    def test_schema_and_duplicate_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "rounds.sqlite3")
            item = {"id": 7, "houseCoefficient": 1.25,
                    "houseCoefficientStr": "1.25", "createdAt": "now"}
            self.assertEqual(insert_rounds(db, [item]), 1)
            self.assertEqual(insert_rounds(db, [item]), 0)
            self.assertEqual(load_rounds(db), [(7, 1.25)])
            with self.assertRaises(ValueError):
                insert_rounds(db, [{"id": 7, "houseCoefficient": 2.0}])
            db.close()

    def test_features_use_only_prior_values(self):
        rows = [(index, 1.1 if index % 2 else 2.0) for index in range(1, 80)]
        dataset = samples(rows, warmup=50)
        first_id, first_features, _, _ = dataset[0]
        self.assertEqual(first_id, 51)
        self.assertEqual(first_features, features([value for _, value in rows[:50]]))

    def test_model_round_trip(self):
        rows = [(index, 1.1 if index % 3 else 2.0) for index in range(1, 300)]
        dataset = samples(rows)
        model = LogisticModel.fit(dataset[:180], epochs=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            model.save(path)
            restored = LogisticModel.load(path)
            self.assertAlmostEqual(model.predict(dataset[-1][1]),
                                   restored.predict(dataset[-1][1]))
        report = score(model, dataset[180:])
        self.assertEqual(report["n"], len(dataset[180:]))


if __name__ == "__main__":
    unittest.main()

