"""Servidor MCP del BCRP. Empiecen aquí: buscar_series ya funciona; completen los TODO hasta que pasen las pruebas.

    uv run pytest                                           # el contrato: las 8 pruebas deben pasar
    uv run server.py                                        # el servidor, por stdio
    claude mcp add --scope project bcrp -- uv run server.py # conectarlo a Claude Code

Las cuentas las hace Python, no el modelo: así cada cifra sale de una llamada que se puede repetir.
"""
import re
import time
import unicodedata
from functools import cache
from pathlib import Path

import httpx
import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

AQUI = Path(__file__).parent
CATALOGO = AQUI / "datos" / "catalogo_bcrp.csv"
URL_CATALOGO = "https://estadisticas.bcrp.gob.pe/estadisticas/series/metadata"
API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SOLO_LECTURA = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP("bcrp", instructions=(
    "Series oficiales del Banco Central de Reserva del Perú (BCRP). Primero busca el código con buscar_series; "
    "luego usa resumen para variaciones, promedios y extremos, y serie para los datos. No calcules a mano: "
    "cita siempre el código de la serie y el periodo."))


def _sin_tildes(s):
    return unicodedata.normalize("NFD", str(s).lower()).encode("ascii", "ignore").decode()


@cache
def catalogo():
    """El catálogo oficial: 17 mil series en unos 7 MB. Se baja una vez y queda en datos/."""
    if not CATALOGO.exists():
        CATALOGO.parent.mkdir(exist_ok=True)
        r = httpx.get(URL_CATALOGO, timeout=120)
        r.raise_for_status()
        CATALOGO.write_text(r.content.decode("latin-1"), encoding="utf-8")
    df = pd.read_csv(CATALOGO, sep=";", dtype=str).fillna("")
    df = df.rename(columns={"Código de serie": "codigo", "Categoría de serie": "categoria", "Grupo de serie": "grupo",
                            "Nombre de serie": "nombre", "Frecuencia": "frecuencia",
                            "Fecha de actualización": "actualizada", "Fecha de inicio": "inicio"})
    df["texto"] = (df.categoria + " " + df.grupo + " " + df.nombre).map(_sin_tildes)
    return df


def _ficha(codigo):
    fila = catalogo().query("codigo == @codigo")
    if fila.empty:
        raise ToolError(f"No existe el código de serie {codigo!r}. Búsquenlo con buscar_series.")
    f = fila.iloc[0]
    return {"codigo": f.codigo, "nombre": f"{f.grupo} - {f.nombre}", "categoria": f.categoria,
            "frecuencia": f.frecuencia, "inicio": f.inicio, "actualizada": f.actualizada}


@mcp.tool(annotations=SOLO_LECTURA)
def buscar_series(texto: str, frecuencia: str | None = None, limite: int = 10) -> list[dict]:
    """Busca series del BCRP por palabras: todas deben aparecer en la categoría, el grupo o el nombre.

    Ejemplos: "ipc 12 meses", "tipo de cambio interbancario venta", "reservas internacionales netas",
    "tasa de referencia", "cobre precio". frecuencia: "Diaria", "Mensual", "Trimestral" o "Anual".
    Devuelve el código, el nombre completo, la frecuencia y desde cuándo hay datos. Si no hay resultados,
    prueben con menos palabras.
    """
    df = catalogo()
    m = pd.Series(True, index=df.index)
    for palabra in _sin_tildes(texto).split():
        m &= df.texto.str.contains(re.escape(palabra))
    if frecuencia:
        m &= df.frecuencia.str.lower() == frecuencia.lower()
    # primero las series vigentes y, entre ellas, las de nombre más corto (las más generales)
    sel = df[m]
    orden = sel.assign(vieja=sel.actualizada < "2025", largo=(sel.grupo + sel.nombre).str.len())
    return [_ficha(c) for c in orden.sort_values(["vieja", "largo"]).head(limite).codigo]


MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


def _pedido(fecha, frecuencia):
    """Convierte "2025-01", "2025-01-15", "2025-T1" o "2025" al formato que pide la API según la frecuencia."""
    partes = re.split(r"[-./]", str(fecha).strip().upper())
    anio = partes[0]
    if frecuencia == "Anual":
        return anio
    if frecuencia == "Diaria":
        mes = partes[1] if len(partes) > 1 else "1"
        dia = partes[2] if len(partes) > 2 else "1"
        return f"{anio}-{int(mes):02d}-{int(dia):02d}"
    resto = partes[1] if len(partes) > 1 else "1"
    if frecuencia == "Trimestral":
        return f"{anio}-{int(resto.lstrip('T'))}"
    return f"{anio}-{int(resto)}"


def _periodo(nombre, frecuencia):
    """Convierte "Ene.2025", "01.Oct.26", "T1.25" o "2025" en "2025-01", "2026-10-01", "2025-T1" o "2025"."""
    if frecuencia == "Diaria":
        dia, mes, anio = nombre.split(".")
        return f"20{anio}-{MESES[mes.lower()]:02d}-{int(dia):02d}"
    if frecuencia == "Mensual":
        mes, anio = nombre.split(".")
        return f"{anio}-{MESES[mes.lower()]:02d}"
    if frecuencia == "Trimestral":
        t, anio = nombre.split(".")
        return f"20{anio}-{t}" if len(anio) == 2 else f"{anio}-{t}"
    return nombre


@cache
def _descargar(url):
    """Pide la URL a la API; si responde HTML (límite de pedidos), espera y reintenta. Guarda en caché lo bajado."""
    for intento in range(5):
        r = httpx.get(url, timeout=60, follow_redirects=True)
        try:
            return r.json()
        except ValueError:
            time.sleep(2 * (intento + 1))
    raise ToolError("El BCRP no respondió con datos. Esperen unos segundos y vuelvan a intentar.")


@mcp.tool(annotations=SOLO_LECTURA)
def serie(codigo: str, desde: str, hasta: str) -> dict:
    """Descarga los datos de una serie del BCRP entre dos fechas, con la URL de la fuente.

    Fechas: "2025-01" (mensual), "2026-10-01" (diaria), "2025-T1" (trimestral) o "2025" (anual).
    Ejemplo: serie("PN01273PM", "2026-01", "2026-09") para la inflación de Lima a 12 meses en 2026.
    Usen buscar_series antes si no saben el código. Para variaciones o promedios usen resumen.
    """
    ficha = _ficha(codigo)
    frec = ficha["frecuencia"]
    fuente = f"{API}/{codigo}/json/{_pedido(desde, frec)}/{_pedido(hasta, frec)}"
    js = _descargar(fuente)
    datos = [{"periodo": _periodo(p["name"], frec), "valor": float(p["values"][0])}
             for p in js.get("periods", []) if p["values"] and p["values"][0] not in ("n.d.", "")]
    return {"codigo": codigo, "nombre": ficha["nombre"], "frecuencia": frec, "datos": datos, "fuente": fuente}


@mcp.tool(annotations=SOLO_LECTURA)
def resumen(codigo: str, desde: str, hasta: str) -> dict:
    """Resume una serie del BCRP entre dos fechas: primer y último dato, cambio, variación %, promedio, máximo y mínimo.

    Úsenla siempre que la pregunta pida una variación, un promedio o un extremo, en vez de calcular a mano.
    Mismas fechas que serie. Ejemplo: resumen("PD04638PD", "2026-10-01", "2026-10-15").
    """
    s = serie(codigo, desde, hasta)
    d = s["datos"]
    if not d:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}.")
    primero, ultimo = d[0], d[-1]
    cambio = ultimo["valor"] - primero["valor"]
    return {"codigo": codigo, "nombre": s["nombre"], "frecuencia": s["frecuencia"], "n": len(d),
            "primero": primero, "ultimo": ultimo, "cambio": round(cambio, 6),
            "variacion_pct": round(100 * cambio / primero["valor"], 4) if primero["valor"] else None,
            "promedio": round(sum(x["valor"] for x in d) / len(d), 6),
            "maximo": max(d, key=lambda x: x["valor"]), "minimo": min(d, key=lambda x: x["valor"]),
            "fuente": s["fuente"]}


INDICADORES_BM = {"pbi_crecimiento": "NY.GDP.MKTP.KD.ZG", "inflacion": "FP.CPI.TOTL.ZG",
                  "desempleo": "SL.UEM.TOTL.ZS", "pbi_per_capita_usd": "NY.GDP.PCAP.CD",
                  "exportaciones_pct_pbi": "NE.EXP.GNFS.ZS", "deuda_publica_pct_pbi": "GC.DOD.TOTL.GD.ZS"}


@mcp.tool(annotations=SOLO_LECTURA)
def comparar_paises(indicador: str = "pbi_crecimiento", paises: str = "PER;CHL;COL;MEX;BRA",
                    desde: int = 2015, hasta: int = 2025) -> dict:
    """Compara al Perú con otros países en un indicador anual del Banco Mundial (el BCRP solo tiene datos del Perú).

    Úsenla cuando la pregunta compare países, por ejemplo «¿creció más el Perú que Chile en 2024?».
    indicador: pbi_crecimiento, inflacion, desempleo, pbi_per_capita_usd, exportaciones_pct_pbi,
    deuda_publica_pct_pbi, o un código del Banco Mundial como "NY.GDP.MKTP.KD.ZG".
    paises: códigos ISO3 separados por ";", por ejemplo "PER;CHL;COL". Devuelve los valores por país y año, la
    fuente y, para cada año, qué país tuvo el valor más alto.
    """
    codigo = INDICADORES_BM.get(indicador, indicador)
    fuente = f"https://api.worldbank.org/v2/country/{paises}/indicator/{codigo}?format=json&date={desde}:{hasta}"
    js = httpx.get(fuente + "&per_page=1000", timeout=60).json()
    if len(js) < 2 or not js[1]:
        raise ToolError(f"El Banco Mundial no tiene datos de {codigo!r} para {paises} entre {desde} y {hasta}.")
    datos = {}
    for x in js[1]:
        if x["value"] is not None:
            datos.setdefault(x["countryiso3code"], {})[x["date"]] = round(x["value"], 4)
    anios = sorted({a for v in datos.values() for a in v})
    lider = {a: max((p for p in datos if a in datos[p]), key=lambda p: datos[p][a]) for a in anios}
    return {"indicador": js[1][0]["indicator"]["value"], "codigo": codigo,
            "datos": {p: dict(sorted(v.items())) for p, v in datos.items()}, "mas_alto_por_anio": lider,
            "fuente": fuente}


@mcp.resource("bcrp://serie/{codigo}", mime_type="application/json")
def ficha_serie(codigo: str) -> dict:
    """La ficha de una serie del BCRP: nombre, categoría, frecuencia, desde cuándo hay datos y última actualización."""
    return _ficha(codigo)


@mcp.prompt
def informe(tema: str) -> str:
    """Pide un informe breve sobre un tema económico del Perú con cifras oficiales del BCRP."""
    return (f"Escribe un informe breve sobre «{tema}» en el Perú con datos oficiales del BCRP.\n"
            "1. Busca las series pertinentes con buscar_series.\n"
            "2. Usa resumen para las variaciones, promedios y extremos, y serie para los datos; no calcules a mano.\n"
            "3. Si compara con otros países, usa comparar_paises.\n"
            "4. Cita el código de la serie y el periodo de cada cifra, y termina con la lista de fuentes (URL).")


if __name__ == "__main__":
    mcp.run()
