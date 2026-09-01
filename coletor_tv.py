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

REGRA DO CORTE
  Grava o fechamento da ultima barra de 1 minuto que terminou
  ANTES das 09:00 BRT. Ou seja, a barra das 08:59.

PRIMEIRO USO
    pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git
    setx TV_USUARIO "seu_usuario"
    setx TV_SENHA   "sua_senha"
    (fechar e reabrir o terminal)

USO
    python coletor_tv.py              # pregao de hoje
    python coletor_tv.py 2026-09-02   # pregao especifico
    python coletor_tv.py --testar     # so mostra, nao grava
================================================================
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone

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

# simbolo, bolsa. Ajuste se o codigo do contrato mudar.
DX = ("DXU2026", "ICEUS")
CME = ("6LV2026", "CME")

CORTE_HORA = 9      # 09:00 BRT
CORTE_MINUTO = 0
BARRAS = 300        # quantas barras de 1 min buscar

BRT = timezone(timedelta(hours=-3))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("tv")


def ultima_antes_do_corte(df, pregao, rotulo):
    """
    Devolve (instante, fechamento) da ultima barra de 1 min que
    fechou antes das 09:00 BRT do pregao pedido.

    O tvdatafeed devolve o indice no fuso do exchange; por isso
    comparamos pela data/hora local convertida, e nao por posicao.
    """
    if df is None or len(df) == 0:
        log.warning(f"{rotulo}: nenhuma barra retornada")
        return None, None

    corte = datetime.fromisoformat(f"{pregao}T{CORTE_HORA:02d}:{CORTE_MINUTO:02d}:00")

    candidatas = []
    for ts, linha in df.iterrows():
        momento = ts.to_pydatetime().replace(tzinfo=None)
        # a barra rotulada HH:MM so fecha em HH:MM:59
        if momento.date().isoformat() != pregao:
            continue
        if momento + timedelta(minutes=1) <= corte:
            candidatas.append((momento, float(linha["close"])))

    if not candidatas:
        log.warning(f"{rotulo}: nenhuma barra do dia {pregao} antes do corte")
        return None, None

    candidatas.sort()
    return candidatas[-1]


def main():
    testar = "--testar" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        pregao = args[0]
    else:
        pregao = datetime.now(BRT).date().isoformat()

    if not USUARIO or not SENHA:
        log.warning(
            "TV_USUARIO/TV_SENHA nao definidos - conectando sem login. "
            "Sem credencial o TradingView limita simbolos e nao entrega "
            "os dados da sua assinatura CME."
        )
        tv = TvDatafeed()
    else:
        tv = TvDatafeed(USUARIO, SENHA)

    payload = {"pregao": pregao}

    # ---- Dolar Index ----
    try:
        df = tv.get_hist(symbol=DX[0], exchange=DX[1],
                         interval=Interval.in_1_minute, n_bars=BARRAS)
        momento, fechamento = ultima_antes_do_corte(df, pregao, "DX")
        if fechamento:
            # fechamento do dia util anterior, para a variacao
            diario = tv.get_hist(symbol=DX[0], exchange=DX[1],
                                 interval=Interval.in_daily, n_bars=5)
            anterior = None
            if diario is not None and len(diario) >= 2:
                for ts, linha in diario.iterrows():
                    d = ts.to_pydatetime().date().isoformat()
                    if d < pregao:
                        anterior = float(linha["close"])
            payload["dx"] = {
                "valor": fechamento,
                "anterior": anterior,
                "contrato": DX[0],
                "instante": momento.replace(tzinfo=BRT).isoformat(),
            }
            var = f"{(fechamento/anterior - 1)*100:+.3f}%" if anterior else "?"
            log.info(f"DX  {momento:%H:%M} = {fechamento} | anterior {anterior} | {var}")
    except Exception as e:
        log.error(f"DX falhou: {e}")

    # ---- Real na CME ----
    try:
        df = tv.get_hist(symbol=CME[0], exchange=CME[1],
                         interval=Interval.in_1_minute, n_bars=BARRAS)
        momento, fechamento = ultima_antes_do_corte(df, pregao, "CME")
        if fechamento:
            payload["real_cme"] = {
                "valor": fechamento,
                "contrato": CME[0],
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
