"""
coletor_tv.py - Painel Dollar Academy
================================================================
Puxa o Dolar Index e o Real na CME do TradingView por PULL
(tvdatafeed), sem alerta e sem webhook.

FUSO HORARIO - RESOLVIDO POR MEDICAO, NAO POR SUPOSICAO
  O tvdatafeed devolve o indice sem fuso, e o fuso de referencia
  varia conforme a conta e o simbolo. Supor "e Nova York" deu
  barras no futuro na primeira versao.

  Agora o script MEDE: pega a barra mais recente e testa
  deslocamentos de -12h a +12h, ficando com o que coloca essa
  barra no passado recente (ate 90 min atras). Como o feed tem
  no maximo ~20 min de atraso, so um deslocamento cabe.

  Fora do horario de pregao a medicao nao e confiavel; nesse caso
  o script avisa e usa o deslocamento informado em --offset.

REGRA DO CORTE
  Fechamento da ultima barra de 1 minuto que terminou ANTES das
  09:00 BRT.

USO
    python coletor_tv.py --testar --bruto     # diagnostico
    python coletor_tv.py                      # grava o pregao de hoje
    python coletor_tv.py 2026-09-02           # pregao especifico
    python coletor_tv.py --offset -1          # forca o deslocamento
================================================================
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

try:
    from tvDatafeed import TvDatafeed, Interval
except ImportError:
    print("tvDatafeed nao instalado. Rode:")
    print("  pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git")
    sys.exit(1)

# ==============================================================

URL_INGEST = "https://uoabiezsuezmhwtbeerm.supabase.co/functions/v1/ingest-mercado"

USUARIO = os.environ.get("TV_USUARIO", "")
SENHA = os.environ.get("TV_SENHA", "")

DX = ("DXU2026", "ICEUS")
CME = ("6LV2026", "CME")

CORTE_HORA = 9
CORTE_MINUTO = 0
BARRAS = 500

BRT = ZoneInfo("America/Sao_Paulo")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("tv")

BRUTO = "--bruto" in sys.argv


def offset_manual():
    if "--offset" in sys.argv:
        i = sys.argv.index("--offset")
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return None


def medir_offset(df, rotulo):
    """
    Descobre quantas horas somar ao indice para chegar em BRT.

    A barra mais recente do feed e, no maximo, ~20 min mais velha
    que agora. Testamos cada deslocamento inteiro e ficamos com o
    que coloca essa barra entre 0 e 90 minutos atras.
    """
    ultima = df.index[-1].to_pydatetime().replace(tzinfo=None)
    agora = datetime.now(BRT).replace(tzinfo=None)

    for h in range(-12, 13):
        atraso = (agora - (ultima + timedelta(hours=h))).total_seconds() / 60
        if 0 <= atraso <= 90:
            log.info(
                f"{rotulo}: deslocamento medido {h:+d}h "
                f"(ultima barra ha {atraso:.0f} min)"
            )
            return h

    log.warning(
        f"{rotulo}: nao consegui medir o fuso - a barra mais recente e "
        f"{ultima:%d/%m %H:%M} no indice e agora sao {agora:%d/%m %H:%M}. "
        f"Fora do pregao isso e esperado; use --offset N para forcar."
    )
    return None


def barras_em_brt(df, offset):
    saida = []
    for ts, linha in df.iterrows():
        m = ts.to_pydatetime().replace(tzinfo=None) + timedelta(hours=offset)
        saida.append((m, float(linha["close"])))
    return saida


def ultima_antes_do_corte(barras, pregao, rotulo):
    corte = datetime.fromisoformat(
        f"{pregao}T{CORTE_HORA:02d}:{CORTE_MINUTO:02d}:00"
    )
    candidatas = [
        (m, c) for m, c in barras
        if m.date().isoformat() == pregao and m + timedelta(minutes=1) <= corte
    ]
    if not candidatas:
        if barras:
            log.warning(
                f"{rotulo}: nenhuma barra de {pregao} antes das 09:00. "
                f"Faixa: {barras[0][0]:%d/%m %H:%M} a {barras[-1][0]:%d/%m %H:%M}"
            )
        return None, None
    candidatas.sort()
    return candidatas[-1]


def puxar(tv, simbolo, bolsa, pregao, rotulo, forcado):
    df = tv.get_hist(symbol=simbolo, exchange=bolsa,
                     interval=Interval.in_1_minute, n_bars=BARRAS)
    if df is None or len(df) == 0:
        log.warning(f"{rotulo}: nenhuma barra retornada")
        return None, None, None

    offset = forcado if forcado is not None else medir_offset(df, rotulo)
    if offset is None:
        return None, None, None

    barras = barras_em_brt(df, offset)

    if BRUTO:
        log.info(f"--- {rotulo}: {len(barras)} barras (horario de Brasilia)")
        for m, c in barras[-8:]:
            log.info(f"    {m:%d/%m %H:%M} -> {c}")

    momento, fechamento = ultima_antes_do_corte(barras, pregao, rotulo)
    return momento, fechamento, offset


def fechamento_anterior(tv, simbolo, bolsa, pregao, offset):
    """Fechamento diario do ultimo pregao anterior ao pedido."""
    try:
        d = tv.get_hist(symbol=simbolo, exchange=bolsa,
                        interval=Interval.in_daily, n_bars=10)
    except Exception as e:
        log.warning(f"{simbolo}: diario falhou ({e})")
        return None
    if d is None or len(d) == 0:
        return None

    anterior = None
    for ts, linha in d.iterrows():
        # no diario o horario nao importa, so a data; o deslocamento
        # pode virar o dia, entao aplicamos e comparamos a data
        dia = (ts.to_pydatetime().replace(tzinfo=None)
               + timedelta(hours=offset)).date().isoformat()
        if dia < pregao:
            anterior = float(linha["close"])
    return anterior


def main():
    testar = "--testar" in sys.argv
    forcado = offset_manual()
    args = [a for a in sys.argv[1:]
            if not a.startswith("--") and a != str(forcado)]
    pregao = args[0] if args else datetime.now(BRT).date().isoformat()

    if not USUARIO or not SENHA:
        log.warning("sem credencial - dados podem vir limitados")
        tv = TvDatafeed()
    else:
        log.info(f"conectando como {USUARIO}")
        tv = TvDatafeed(USUARIO, SENHA)

    payload = {"pregao": pregao}

    try:
        momento, fechamento, offset = puxar(tv, DX[0], DX[1], pregao, "DX", forcado)
        if fechamento:
            anterior = fechamento_anterior(tv, DX[0], DX[1], pregao, offset)
            payload["dx"] = {
                "valor": fechamento, "anterior": anterior,
                "contrato": DX[0],
                "instante": momento.replace(tzinfo=BRT).isoformat(),
            }
            var = f"{(fechamento/anterior - 1)*100:+.3f}%" if anterior else "?"
            log.info(f"DX  {momento:%H:%M} = {fechamento} | anterior {anterior} | {var}")
    except Exception as e:
        log.error(f"DX falhou: {e}")

    try:
        momento, fechamento, _ = puxar(tv, CME[0], CME[1], pregao, "CME", forcado)
        if fechamento:
            payload["real_cme"] = {
                "valor": fechamento, "contrato": CME[0],
                "instante": momento.replace(tzinfo=BRT).isoformat(),
            }
            log.info(
                f"CME {momento:%H:%M} = {fechamento} "
                f"-> {1/fechamento*1000:.2f} na escala do dolar"
            )
    except Exception as e:
        log.error(f"CME falhou: {e}")

    if "dx" not in payload and "real_cme" not in payload:
        log.error("nada capturado - nao envia")
        sys.exit(1)

    if testar:
        log.info("modo teste: nada foi gravado")
        log.info(payload)
        return

    r = requests.post(URL_INGEST, json=payload, timeout=30)
    log.info(f"ingest: HTTP {r.status_code} {r.text[:300]}")
    if r.status_code != 200:
        sys.exit(1)


if __name__ == "__main__":
    main()
