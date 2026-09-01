"""
backfill_bdi.py - Painel Dollar Academy
================================================================
Roda o coletor do BDI para um intervalo de datas.

Uso:
    python backfill_bdi.py 2025-12-10 2026-08-31

Pula fins de semana e feriados automaticamente (o boletim nao
existe nesses dias, retorna 404 e o script segue).

Usa a mesma logica de extracao do coletor diario (coletor_bdi.
extrair), entao qualquer correcao la vale aqui tambem.

ATENCAO AO ALCANCE
  O formato atual do BDI_03-4 comecou quando a B3 migrou os
  "Ajustes do Pregao" para o boletim, em 10/12/2025. Antes disso
  o capitulo tem outra estrutura e este parser nao funciona.

Baixa ~9 MB por pregao.
================================================================
"""

import sys
import time
import logging
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import coletor_bdi as c

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("backfill")

PARALELO = 3
PAUSA = 0.5


def um_dia(pregao: str):
    """Processa um pregao. Devolve (pregao, resultado)."""
    try:
        raw = c.baixar(pregao)
    except FileNotFoundError:
        return pregao, "sem boletim"
    except Exception as e:
        return pregao, f"erro no download: {e}"

    try:
        paginas = c.achar_paginas(raw)
        if not paginas:
            return pregao, "nenhuma pagina de cotacao"

        registros = c.ler_linhas(raw, paginas)
        payload, alertas = c.extrair(registros, pregao)

        if "di1" not in payload and "dol" not in payload:
            return pregao, f"nada extraido ({len(registros)} linhas lidas)"

        r = requests.post(
            c.URL_INGEST,
            headers={"X-Painel-Token": c.TOKEN,
                     "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            return pregao, f"ingest HTTP {r.status_code}"

        partes = []
        if "di1" in payload:
            partes.append(f"{payload['di1']['contrato']} {payload['di1']['taxa']}")
        if "dol" in payload:
            partes.append(f"{payload['dol']['contrato']} {payload['dol']['ajuste']}")
        return pregao, "ok: " + " | ".join(partes)

    except Exception as e:
        return pregao, f"erro no parse: {e}"


def main():
    if not c.TOKEN:
        log.error("PAINEL_TOKEN nao definido")
        sys.exit(1)

    if len(sys.argv) < 3:
        log.error("uso: python backfill_bdi.py AAAA-MM-DD AAAA-MM-DD")
        sys.exit(1)

    inicio = date.fromisoformat(sys.argv[1])
    fim = date.fromisoformat(sys.argv[2])

    limite = date(2025, 12, 10)
    if inicio < limite:
        log.warning(
            f"{inicio} e anterior a {limite}, quando o formato do BDI mudou. "
            f"Ajustando o inicio para {limite}."
        )
        inicio = limite

    dias = []
    d = inicio
    while d <= fim:
        if d.weekday() < 5:
            dias.append(d.isoformat())
        d += timedelta(days=1)

    log.info(f"{len(dias)} dias uteis de {inicio} a {fim}")
    t0 = time.time()

    ok = vazios = erros = 0
    with ThreadPoolExecutor(max_workers=PARALELO) as ex:
        futuros = {}
        for i, dia in enumerate(dias):
            futuros[ex.submit(um_dia, dia)] = dia
            if i % PARALELO == 0:
                time.sleep(PAUSA)

        for n, fut in enumerate(as_completed(futuros), 1):
            pregao, resultado = fut.result()
            if resultado.startswith("ok"):
                ok += 1
                log.info(f"[{n}/{len(dias)}] {pregao} {resultado}")
            elif resultado == "sem boletim":
                vazios += 1
            else:
                erros += 1
                log.warning(f"[{n}/{len(dias)}] {pregao} {resultado}")

    print()
    log.info("=" * 55)
    log.info(f"gravados : {ok}")
    log.info(f"sem boletim (feriado?): {vazios}")
    log.info(f"com erro : {erros}")
    log.info(f"tempo    : {round(time.time() - t0)}s")


if __name__ == "__main__":
    main()
