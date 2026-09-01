"""
coletor_bdi.py - Painel Dollar Academy
================================================================
Le o Boletim Diario da B3 (capitulo 03-4, "Negocios consolidados
do pregao") e extrai:

  DI1  -> taxa de fechamento e taxa do ajuste do contrato de
          MAIOR numero de negocios (hoje o DI1F29)
  DOL  -> ajuste e ajuste D-1 do contrato corrente
  WDO  -> usado so para validar (o ajuste tem que ser igual ao DOL)

Roda no GitHub Actions, nao precisa da maquina do Rafael ligada.

REGRA DE SEGURANCA
  As tres ultimas colunas do PDF (negocios / contratos / volume)
  colam entre si quando os numeros sao grandes. Sao lidas apenas
  para ordenar por liquidez, NUNCA gravadas.
================================================================
"""

import io
import os
import re
import sys
import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta

import requests
import pdfplumber
from pypdf import PdfReader

# ==============================================================

URL_INGEST = "https://uoabiezsuezmhwtbeerm.supabase.co/functions/v1/ingest-bdi"
TOKEN = os.environ.get("PAINEL_TOKEN", "")

COD_MES = "FGHJKMNQUVXZ"  # jan..dez
PREFIXOS = ("DI1", "DOL", "WDO")
ALVO = re.compile(r"^(DI1|DOL|WDO)[FGHJKMNQUVXZ]\d{2}$")

# nomes das colunas, na ordem em que aparecem no boletim.
# so as 15 primeiras sao confiaveis (ver REGRA DE SEGURANCA)
COLUNAS = [
    "instrumento", "isin", "segmento", "abertura", "minimo", "maximo",
    "medio", "fechamento", "oscilacao", "ajuste", "ajuste_ref",
    "ajuste_d1", "preco_ref", "variacao", "ajuste_contrato",
]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bdi")


def num(txt):
    """'5.219,3930' -> 5219.393 ; '-' -> None"""
    if txt is None:
        return None
    t = str(txt).strip()
    if t in ("-", "", "--"):
        return None
    try:
        return float(t.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def contrato_dolar(d: date) -> str:
    """
    DOL vence no 1o dia util do mes; durante o mes corrente o
    contrato negociado e o do mes SEGUINTE.
    """
    m = d.month  # 1..12 -> indice do mes seguinte em COD_MES
    ano = d.year + (1 if m > 11 else 0)
    return f"DOL{COD_MES[m % 12]}{str(ano)[2:]}"


def baixar(pregao: str) -> bytes:
    url = (
        "https://arquivos.b3.com.br/bdi/download/bdi/"
        f"{pregao}/BDI_03-4_{pregao.replace('-', '')}.pdf"
    )
    log.info(f"baixando {url}")
    r = requests.get(url, timeout=240)
    if r.status_code == 404:
        raise FileNotFoundError("boletim ainda nao publicado")
    r.raise_for_status()
    log.info(f"ok - {len(r.content) / 1e6:.1f} MB")
    return r.content


def achar_paginas(raw: bytes):
    """Localiza as paginas com os contratos usando pypdf (rapido)."""
    reader = PdfReader(io.BytesIO(raw))
    achadas = []
    for p in range(1, len(reader.pages) + 1):
        txt = reader.pages[p - 1].extract_text() or ""
        if any(re.search(rf"\b{x}[FGHJKMNQUVXZ]\d{{2}}\s+BR", txt) for x in PREFIXOS):
            achadas.append(p)
        if len(achadas) >= 3 and p > max(achadas) + 3:
            break
    log.info(f"paginas de cotacao: {achadas}")
    return achadas


def ler_linhas(raw: bytes, paginas):
    """Parse por coordenada: as colunas numericas alinham a direita."""
    registros = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for p in paginas:
            page = pdf.pages[p - 1]
            linhas = {}
            for w in page.extract_words():
                linhas.setdefault(round(w["top"] / 3), []).append(w)

            dados = []
            for k in sorted(linhas):
                ws = sorted(linhas[k], key=lambda w: w["x0"])
                if any(ALVO.match(w["text"]) for w in ws[:2]):
                    dados.append(ws)
            if not dados:
                continue

            freq = Counter()
            for ws in dados:
                for w in ws:
                    freq[round(w["x1"])] += 1
            bordas = sorted(x for x, n in freq.items() if n >= max(3, len(dados) * 0.3))
            fundidas = []
            for x in bordas:
                if fundidas and x - fundidas[-1] <= 5:
                    fundidas[-1] = x
                else:
                    fundidas.append(x)
            if len(fundidas) < 12:
                continue

            for ws in dados:
                celulas = {}
                for w in ws:
                    i = min(range(len(fundidas)), key=lambda j: abs(fundidas[j] - w["x1"]))
                    if abs(fundidas[i] - w["x1"]) > 12:
                        continue
                    celulas.setdefault(i, []).append(w["text"])

                campos = [" ".join(celulas.get(i, ["-"])) for i in range(len(fundidas))]
                inst = next((c for c in campos[:3] if ALVO.match(c)), None)
                if not inst:
                    continue

                r = dict(zip(COLUNAS, campos[: len(COLUNAS)]))
                r["instrumento"] = inst
                r["_pagina"] = p
                # negocios: penultima ou antepenultima coluna, so p/ ordenar
                r["_neg"] = num(campos[-3]) or num(campos[-2]) or 0
                registros.append(r)
    return registros


def coerente(r) -> bool:
    """minimo <= fechamento <= maximo"""
    mi, fe, ma = num(r.get("minimo")), num(r.get("fechamento")), num(r.get("maximo"))
    if None in (mi, fe, ma):
        return False
    return mi <= fe <= ma


def main():
    if not TOKEN:
        log.error("PAINEL_TOKEN nao definido")
        sys.exit(1)

    # pregao: o argumento, ou o dia corrente em BRT
    if len(sys.argv) > 1 and sys.argv[1].strip():
        pregao = sys.argv[1].strip()
    else:
        pregao = (datetime.utcnow() - timedelta(hours=3)).date().isoformat()

    alertas = []

    try:
        raw = baixar(pregao)
    except FileNotFoundError:
        log.warning(f"{pregao}: boletim ainda nao publicado - saindo sem erro")
        sys.exit(0)

    paginas = achar_paginas(raw)
    if not paginas:
        log.error("nenhuma pagina de cotacao encontrada")
        sys.exit(1)

    registros = ler_linhas(raw, paginas)
    log.info(f"{len(registros)} linhas lidas")

    # ---- DI1: o de maior numero de negocios ----
    di1 = [r for r in registros if r["instrumento"].startswith("DI1") and coerente(r)]
    di1.sort(key=lambda r: r["_neg"], reverse=True)
    saida_di1 = None
    if di1:
        r = di1[0]
        saida_di1 = {
            "contrato": r["instrumento"],
            "taxa": num(r["fechamento"]),
            "ajuste_taxa": num(r["ajuste_ref"]),
            "negocios": r["_neg"],
        }
        log.info(f"DI1 -> {saida_di1}")
    else:
        alertas.append("di1: nenhum contrato coerente encontrado")

    # ---- DOL: contrato corrente ----
    alvo_dol = contrato_dolar(date.fromisoformat(pregao))
    saida_dol = None
    achado = next((r for r in registros if r["instrumento"] == alvo_dol), None)
    if achado:
        saida_dol = {
            "contrato": alvo_dol,
            "ajuste": num(achado["ajuste"]),
            "ajuste_d1": num(achado["ajuste_d1"]),
        }
        log.info(f"DOL -> {saida_dol}")

        # validacao: o WDO do mesmo vencimento tem que ter o mesmo ajuste
        alvo_wdo = "W" + alvo_dol[1:]
        w = next((r for r in registros if r["instrumento"] == alvo_wdo), None)
        if w:
            if num(w["ajuste"]) != saida_dol["ajuste"]:
                alertas.append(
                    f"wdo: ajuste {num(w['ajuste'])} difere do dol {saida_dol['ajuste']}"
                )
        else:
            alertas.append(f"wdo: {alvo_wdo} nao encontrado (validacao pulada)")
    else:
        alertas.append(f"dol: contrato {alvo_dol} nao encontrado no boletim")

    if not saida_di1 and not saida_dol:
        log.error("nada extraido - nao envia")
        sys.exit(1)

    payload = {"pregao": pregao, "alertas": alertas}
    if saida_di1:
        payload["di1"] = saida_di1
    if saida_dol:
        payload["dol"] = saida_dol

    log.info(json.dumps(payload, ensure_ascii=False))

    r = requests.post(
        URL_INGEST,
        headers={"X-Painel-Token": TOKEN, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    log.info(f"ingest: HTTP {r.status_code} {r.text[:300]}")
    if r.status_code != 200:
        sys.exit(1)


if __name__ == "__main__":
    main()
