"""
coletor_tv.py - Painel Dollar Academy
================================================================
Puxa o Dolar Index e o Real na CME do TradingView por PULL
(tvdatafeed), sem alerta e sem webhook.

Por que assim: alertas do TradingView falham de varias formas -
dependem de tick para serem avaliados, congelam os parametros no
momento da criacao e ja deixaram de disparar em producao. Puxar
o dado quando queremos elimina toda essa classe de problema.

A conta assina os dados da CME, entao o 6L deve vir em tempo
real. O DX e da ICE (sem assinatura), entao vem com ~10 min de
atraso - ainda melhor que os 20 min do Yahoo.

FUSO HORARIO - CUIDADO
  O tvdatafeed devolve o indice no fuso da BOLSA (Nova York),
  nao em Brasilia. 08:59 BRT = 07:59 em Nova York no horario de
  verao americano, 06:59 fora dele. Por isso o script converte
  explicitamente em vez de comparar a hora crua.

REGRA DO CORTE
  Grava o fechamento da ultima barra de 1 minuto que terminou
  ANTES das 09:00 BRT. Ou seja, a barra das 08:59.

PRIMEIRO USO
    pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git
    setx TV_USUARIO "seu_login_real"
    setx TV_SENHA   "sua_senha_real"
    FECHE e reabra o terminal (setx so vale para processos novos)

USO
    python coletor_tv.py              # pregao de hoje
    python coletor_tv.py 2026-09-02   # pregao especifico
    python coletor_tv.py --testar     # so mostra, nao grava
    python coletor_tv.py --bruto      # lista as barras lidas
================================================================
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from tvDatafeed import TvDatafeed, Interval
except ImportError:
    print("tvDatafeed nao instalado. Rode:")
    print("  pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git")
    sys.exit(1)

# ==============================================================
# CONFIGURACAO
# ==============================================================

URL_INGEST = "https://uoabiezsuezmhwtbeerm.supabase.co/functions/v1/ingest-mercado"

USUARIO = os.environ.get("TV_USUARIO", "")
SENHA = os.environ.get("TV_SENHA", "")

# (simbolo, bolsa, fuso da bolsa)
DX = ("DXU2026", "ICEUS", "America/New_York")
CME = ("6LV2026", "CME", "America/Chicago")

CORTE_HORA = 9      # 09:00 BRT
CORTE_MINUTO = 0
BARRAS = 500

BRT = ZoneInfo("America/Sao_Paulo")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("tv")

MOSTRAR_BRUTO = "--bruto" in sys.argv


def ultima_antes_do_corte(df, pregao, fuso_bolsa, rotulo):
    """
    Devolve (instante_brt, fechamento) da ultima barra de 1 min
    que fechou antes das 09:00 BRT do pregao pedido.

    O indice vem sem fuso, expresso no horario da bolsa. Marcamos
    o fuso da bolsa e convertemos para Brasilia antes de comparar.
    """
    if df is None or len(df) == 0:
        log.warning(f"{rotulo}: nenhuma barra retornada")
        return None, None

    tz_bolsa = ZoneInfo(fuso_bolsa)
    corte = datetime.fromisoformat(
        f"{pregao}T{CORTE_HORA:02d}:{CORTE_MINUTO:02d}:00"
    ).replace(tzinfo=BRT)

    candidatas = []
    todas = []
    for ts, linha in df.iterrows():
        cru = ts.to_pydatetime()
        if cru.tzinfo is None:
            cru = cru.replace(tzinfo=tz_bolsa)
        momento = cru.astimezone(BRT)
        todas.append((momento, float(linha["close"])))
        # a barra rotulada HH:MM so fecha em HH:MM:59
        if momento.date().isoformat() == pregao and \
                momento + timedelta(minutes=1) <= corte:
            candidatas.append((momento, float(linha["close"])))

    if MOSTRAR_BRUTO:
        log.info(f"--- {rotulo}: {len(todas)} barras (horario de Brasilia)")
        for m, c in todas[-12:]:
            log.info(f"    {m:%d/%m %H:%M} -> {c}")

    if not candidatas:
        if todas:
            primeira, ultima = todas[0][0], todas[-1][0]
            log.warning(
                f"{rotulo}: nenhuma barra de {pregao} antes das 09:00 BRT. "
                f"Faixa recebida: {primeira:%d/%m %H:%M} a {ultima:%d/%m %H:%M}"
            )
        return None, None

    candidatas.sort()
    return candidatas[-1]


def fechamento_anterior(tv, simbolo, bolsa, pregao):
    """Fechamento diario do ultimo pregao anterior ao pedido."""
    try:
        diario = tv.get_hist(symbol=simbolo, exchange=bolsa,
                             interval=Interval.in_daily, n_bars=10)
    except Exception as e:
        log.warning(f"{simbolo}: diario falhou ({e})")
        return None
    if diario is None or len(diario) == 0:
        return None
    anterior = None
    for ts, linha in diario.iterrows():
        if ts.to_pydatetime().date().isoformat() < pregao:
            anterior = float(linha["close"])
    return anterior


def main():
    testar = "--testar" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pregao = args[0] if args else datetime.now(BRT).date().isoformat()

    if not USUARIO or not SENHA:
        log.warning(
            "TV_USUARIO/TV_SENHA nao definidos - conectando sem login. "
            "Sem credencial o TradingView limita simbolos e nao entrega "
            "os dados da sua assinatura CME. Rode setx e REABRA o terminal."
        )
        tv = TvDatafeed()
    else:
        log.info(f"conectando como {USUARIO}")
        tv = TvDatafeed(USUARIO, SENHA)

    payload = {"pregao": pregao}

    # ---- Dolar Index ----
    try:
        df = tv.get_hist(symbol=DX[0], exchange=DX[1],
                         interval=Interval.in_1_minute, n_bars=BARRAS)
        momento, fechamento = ultima_antes_do_corte(df, pregao, DX[2], "DX")
        if fechamento:
            anterior = fechamento_anterior(tv, DX[0], DX[1], pregao)
            payload["dx"] = {
                "valor": fechamento,
                "anterior": anterior,
                "contrato": DX[0],
                "instante": momento.isoformat(),
            }
            var = f"{(fechamento/anterior - 1)*100:+.3f}%" if anterior else "?"
            log.info(
                f"DX  {momento:%H:%M} BRT = {fechamento} | "
                f"anterior {anterior} | {var}"
            )
    except Exception as e:
        log.error(f"DX falhou: {e}")

    # ---- Real na CME ----
    try:
        df = tv.get_hist(symbol=CME[0], exchange=CME[1],
                         interval=Interval.in_1_minute, n_bars=BARRAS)
        momento, fechamento = ultima_antes_do_corte(df, pregao, CME[2], "CME")
        if fechamento:
            payload["real_cme"] = {
                "valor": fechamento,
                "contrato": CME[0],
                "instante": momento.isoformat(),
            }
            log.info(
                f"CME {momento:%H:%M} BRT = {fechamento} "
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
