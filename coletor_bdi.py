"""
coletor_bdi.py - Painel Dollar Academy
================================================================
Le o Boletim Diario da B3 (capitulo 03-4, "Negocios consolidados
do pregao") e extrai:

  DI1  -> taxa de fechamento e taxa do ajuste
  DOL  -> ajuste e ajuste D-1
  WDO  -> usado so para validar (o ajuste tem que ser igual ao DOL)

EM AMBOS OS CASOS o contrato escolhido e o de MAIOR NUMERO DE
NEGOCIOS do dia, nao um contrato calculado por calendario.

  Por que: a primeira versao calculava o contrato do DOL pela
  regra "mes seguinte ao corrente" e exigia aquele codigo exato.
  No backfill isso falhou em quase 100 pregoes (DOLM26, DOLK26,
  DOLH26...), enquanto o DI1 - que ja era escolhido por liquidez -
  nao falhou nenhuma vez. Liquidez e mais robusto e dispensa
  saber o calendario de vencimento.

Roda no GitHub Actions, nao precisa de maquina ligada.

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
from datetime import datetime, timedelta, timezone

import requests
import pdfplumber
from pypdf import PdfReader

# ==============================================================

URL_INGEST = "https://uoabiezsuezmhwtbeerm.supabase.co/functions/v1/ingest-bdi"
TOKEN = os.environ.get("PAINEL_TOKEN", "")

PREFIXOS = ("DI1", "DOL", "WDO")
ALVO = re.compile(r"^(DI1|DOL|WDO)[FGHJKMNQUVXZ]\d{2}$")

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


def baixar(pregao: str) -> bytes:
    url = (
        "https://arquivos.b3.com.br/bdi/download/bdi/"
        f"{pregao}/BDI_03-4_{pregao.replace('-', '')}.pdf"
    )
    log.info(f"baixando {url}")
    r = requests.get(url, timeout=240)

    # A B3 responde 404 OU 500 quando o boletim ainda nao existe -
    # o 500 derrubou o job de 01/09/2026 as 21h36. Tratamos os dois
    # como "ainda nao publicado": o retry mais tarde resolve.
    if r.status_code in (404, 500, 502, 503, 504):
        raise FileNotFoundError(f"boletim indisponivel (HTTP {r.status_code})")
    r.raise_for_status()

    # PDF valido comeca com %PDF; pagina de erro em HTML nao
    if r.content[:4] != b"%PDF":
        raise FileNotFoundError(
            f"resposta nao e um PDF ({r.headers.get('Content-Type')})"
        )

    log.info(f"ok - {len(r.content) / 1e6:.1f} MB")
    return r.content


def achar_paginas(raw: bytes):
    """
    Localiza as paginas com os contratos usando pypdf.

    Varre o documento inteiro de proposito: as paginas de DI1 e de
    DOL/WDO ficam em blocos separados e distantes. A versao antiga
    parava depois de achar tres paginas e por isso perdia o DOL em
    boa parte dos pregoes.
    """
    reader = PdfReader(io.BytesIO(raw))
    achadas = []
    for p in range(1, len(reader.pages) + 1):
        txt = reader.pages[p - 1].extract_text() or ""
        if any(re.search(rf"\b{x}[FGHJKMNQUVXZ]\d{{2}}\s+BR", txt) for x in PREFIXOS):
            achadas.append(p)
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
                    i = min(range(len(fundidas)),
                            key=lambda j: abs(fundidas[j] - w["x1"]))
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
                r["_neg"] = num(campos[-3]) or num(campos[-2]) or 0
                registros.append(r)
    return registros


def coerente(r) -> bool:
    """minimo <= fechamento <= maximo"""
    mi, fe, ma = num(r.get("minimo")), num(r.get("fechamento")), num(r.get("maximo"))
    if None in (mi, fe, ma):
        return False
    return mi <= fe <= ma


def mais_liquido(registros, prefixo, exigir_coerencia=True):
    """Contrato de maior numero de negocios entre os do prefixo."""
    cands = [r for r in registros if r["instrumento"].startswith(prefixo)]
    if exigir_coerencia:
        coerentes = [r for r in cands if coerente(r)]
        if coerentes:
            cands = coerentes
    if not cands:
        return None
    return max(cands, key=lambda r: r["_neg"])


def extrair(registros, pregao):
    """Monta o payload a partir das linhas lidas. Devolve (payload, alertas)."""
    alertas = []
    payload = {"pregao": pregao}

    r = mais_liquido(registros, "DI1")
    if r:
        payload["di1"] = {
            "contrato": r["instrumento"],
            "taxa": num(r["fechamento"]),
            "ajuste_taxa": num(r["ajuste_ref"]),
            "negocios": r["_neg"],
        }
    else:
        alertas.append("di1: nenhum contrato encontrado")

    d = mais_liquido(registros, "DOL")
    if d:
        payload["dol"] = {
            "contrato": d["instrumento"],
            "ajuste": num(d["ajuste"]),
            "ajuste_d1": num(d["ajuste_d1"]),
        }
        # o WDO do mesmo vencimento tem que ter o mesmo ajuste
        alvo_wdo = "WDO" + d["instrumento"][3:]
        w = next((x for x in registros if x["instrumento"] == alvo_wdo), None)
        if w:
            if num(w["ajuste"]) != payload["dol"]["ajuste"]:
                alertas.append(
                    f"wdo: ajuste {num(w['ajuste'])} difere do dol "
                    f"{payload['dol']['ajuste']}"
                )
        else:
            alertas.append(f"wdo: {alvo_wdo} nao encontrado (validacao pulada)")
    else:
        alertas.append("dol: nenhum contrato encontrado")

    payload["alertas"] = alertas
    return payload, alertas


def main():
    if not TOKEN:
        log.error("PAINEL_TOKEN nao definido")
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1].strip():
        pregao = sys.argv[1].strip()
    else:
        pregao = (datetime.now(timezone.utc) - timedelta(hours=3)).date().isoformat()

    try:
        raw = baixar(pregao)
    except FileNotFoundError as e:
        log.warning(f"{pregao}: {e} - saindo sem erro; o retry mais tarde tenta de novo")
        sys.exit(0)

    paginas = achar_paginas(raw)
    if not paginas:
        log.error("nenhuma pagina de cotacao encontrada")
        sys.exit(1)

    registros = ler_linhas(raw, paginas)
    log.info(f"{len(registros)} linhas lidas")

    payload, _ = extrair(registros, pregao)
    if "di1" not in payload and "dol" not in payload:
        log.error("nada extraido - nao envia")
        sys.exit(1)

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
