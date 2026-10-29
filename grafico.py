"""Figura del informe: porcentaje de aciertos, errores y «no sé» por modo, a partir de resultados_web.csv.

    uv run grafico.py
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

AQUI = Path(__file__).parent
res = pd.read_csv(AQUI / "resultados_web.csv")
tabla = res.groupby("modo").resultado.value_counts(normalize=True).unstack(fill_value=0) * 100
tabla = tabla.reindex(["memoria", "web", "mcp"]).reindex(columns=["acierta", "se equivoca", "no sabe"], fill_value=0)

fig, ax = plt.subplots(figsize=(7, 4))
izq = pd.Series(0.0, index=tabla.index)
for col, color in zip(tabla.columns, ["#2a7d4f", "#c0392b", "#b0b0b0"]):
    ax.barh(tabla.index, tabla[col], left=izq, color=color, label=col)
    for y, (x0, w) in enumerate(zip(izq, tabla[col])):
        if w >= 8:
            ax.text(x0 + w / 2, y, f"{w:.0f} %", ha="center", va="center", color="white", fontsize=10)
    izq += tabla[col]
ax.invert_yaxis()
ax.set_xlim(0, 100)
ax.set_xlabel("% de las 10 preguntas")
ax.set_title("Claude Haiku: ¿acierta la cifra oficial del BCRP?")
ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.38), ncol=3, frameon=False)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(AQUI / "grafico.png", dpi=150)
print(tabla.round(0))
