"""Leakage-safe standard-library modeling for paper-only crash-game audits."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


FEATURE_NAMES = [
    "rate_ge_1_5_10", "rate_ge_1_5_50", "rate_ge_2_50",
    "mean_log_20", "low_streak", "last_log_multiplier",
]


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1 + exp)


def features(history: list[float]) -> list[float]:
    def rate(window: int, threshold: float) -> float:
        values = history[-window:]
        return sum(value >= threshold for value in values) / len(values) if values else 0.5
    recent = history[-20:]
    streak = 0
    for value in reversed(history):
        if value >= 1.5:
            break
        streak += 1
    return [
        rate(10, 1.5), rate(50, 1.5), rate(50, 2.0),
        sum(math.log(min(value, 100)) for value in recent) / len(recent) if recent else 0.0,
        min(streak, 20) / 20,
        math.log(min(history[-1], 100)) if history else 0.0,
    ]


def samples(rows: list[tuple[int, float]], target: float = 1.5, warmup: int = 50):
    output = []
    history: list[float] = []
    for round_id, multiplier in rows:
        if len(history) >= warmup:
            output.append((round_id, features(history), int(multiplier >= target), multiplier))
        history.append(multiplier)
    return output


@dataclass
class LogisticModel:
    weights: list[float]
    means: list[float]
    scales: list[float]
    target: float = 1.5

    def predict(self, values: list[float]) -> float:
        normalized = [(v-m)/s for v, m, s in zip(values, self.means, self.scales)]
        return sigmoid(self.weights[0] + sum(w*v for w, v in zip(self.weights[1:], normalized)))

    @classmethod
    def fit(cls, rows, target: float = 1.5, epochs: int = 40):
        width = len(rows[0][1])
        means = [sum(row[1][i] for row in rows) / len(rows) for i in range(width)]
        scales = []
        for i, mean in enumerate(means):
            variance = sum((row[1][i] - mean) ** 2 for row in rows) / len(rows)
            scales.append(max(math.sqrt(variance), 1e-6))
        normalized = [([(v-means[i])/scales[i] for i, v in enumerate(x)], y) for _, x, y, _ in rows]
        weights = [0.0] * (width + 1)
        for epoch in range(epochs):
            rate = 0.03 / (1 + epoch * 0.15)
            for x, y in normalized:
                p = sigmoid(weights[0] + sum(w*v for w, v in zip(weights[1:], x)))
                error = p - y
                weights[0] -= rate * error
                for index, value in enumerate(x, 1):
                    weights[index] -= rate * (error * value + 0.001 * weights[index])
        return cls(weights, means, scales, target)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def score(model: LogisticModel, rows) -> dict:
    predictions = [(model.predict(x), y) for _, x, y, _ in rows]
    actual = sum(y for _, y in predictions) / len(predictions)
    brier = sum((p-y) ** 2 for p, y in predictions) / len(predictions)
    return {
        "n": len(predictions), "actual_rate": actual,
        "mean_probability": sum(p for p, _ in predictions) / len(predictions),
        "brier": brier, "baseline_brier": actual * (1-actual),
    }

