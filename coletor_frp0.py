"""
coletor_frp0.py - Painel Dollar Academy
================================================================
Le o FRP0 da planilha ABERTA no Excel (valor vivo do RTD do Profit)
e empurra para a Edge Function ingest-frp0.

NAO precisa configurar celula: o script varre as planilhas abertas,
acha a linha cujo primeiro campo e "FRP0" e localiza as colunas
"Hora" e "Ultimo" pelo cabecalho.

PROTECAO IMPORTANTE
  Quando o Profit e o Black Arrow disputam o servidor RTD, o Excel
  passa a receber ZERO em vez de erro. Zero gravado como preco
  produziria uma PTAX Futuro igual a PTAX spot, sem nada indicar
  o problema. Por isso: valor zerado ou hora zerada NAO sao
  enviados, e o script avisa na tela.

  Ordem que funciona: abrir o Profit ANTES do Black Arrow.

Requisitos:  pip install pywin32 requests
Token:       setx PAINEL_FRP0_TOKEN "seu_token"  (e reabrir o terminal)
================================================================
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

import requests
import win32com.client

# ==============================================================
# CONFIGURACAO
# ==============================================================

URL_INGEST = "https://uoabiezsuezmhwtbeerm.supabase.co/functions/v1/ingest-frp0"
TOKEN = os.environ.get("PAINEL_FRP0_TOKEN", "")

ATIVO = "FRP0"          # valor procurado na primeira coluna
COL_HORA = "hora"       # nomes procurados no cabecalho
COL_VALOR = "ultimo"

JANELA_INICIO = "09:55"
JANELA_FIM = "10:35"
INTERVALO_SEG = 15
ENVIO_SEG = 60

LOG_DIR = os.path.join(os.path.expanduser("~"), "logs_painel")

# ==============================================================

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, f"frp0_{datetime.now():%Y%m}.log"),
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("frp0")

BRT = timezone(timedelta(hours=-3))


def sem_acento(t):
    tabela = str.maketrans("aaaaeeiooouuc", "aaaaeeiooouuc")
    t = str(t or "").strip().lower()
    for de, para in (("\u00fa", "u"), ("\u00e1", "a"), ("\u00e9", "e"),
                     ("\u00ed", "i"), ("\u00f3", "o"), ("\u00e2", "a"),
                     ("\u00ea", "e"), ("\u00f4", "o"), ("\u00e3", "a"),
                     ("\u00f5", "o"), ("\u00e7", "c")):
        t = t.replace(de, para)
    return t.translate(tabela)


def localizar():
    """
    Varre as planilhas abertas procurando a linha do FRP0.
    Devolve (aba, linha, coluna_hora, coluna_valor).
    """
    excel = win32com.client.GetActiveObject("Excel.Application")
    if excel.Workbooks.Count == 0:
        raise RuntimeError("o Excel esta aberto, mas sem nenhuma planilha")

    for i in range(excel.Workbooks.Count):
        wb = excel.Workbooks.Item(i + 1)
        for j in range(wb.Worksheets.Count):
            aba = wb.Worksheets.Item(j + 1)
            try:
                usada = aba.UsedRange
                nlin = min(usada.Rows.Count, 200)
                ncol = min(usada.Columns.Count, 40)
            except Exception:
                continue

            linha_ativo = None
            for r in range(1, nlin + 1):
                if sem_acento(aba.Cells(r, 1).Value) == ATIVO.lower():
                    linha_ativo = r
                    break
            if not linha_ativo:
                continue

            # cabecalho: procura acima da linha do ativo
            col_hora = col_valor = None
            for r in range(1, linha_ativo):
                for c in range(1, ncol + 1):
                    nome = sem_acento(aba.Cells(r, c).Value)
                    if nome == COL_HORA:
                        col_hora = c
                    elif nome == COL_VALOR:
                        col_valor = c
                if col_hora and col_valor:
                    break

            if col_valor:
                log.info(
                    f"encontrado em '{wb.Name}' / aba '{aba.Name}' "
                    f"linha {linha_ativo}, valor col {col_valor}, "
                    f"hora col {col_hora or 'ausente'}"
                )
                return aba, linha_ativo, col_hora, col_valor

    raise RuntimeError(
        f"nao achei uma linha '{ATIVO}' na primeira coluna de nenhuma "
        "planilha aberta. Abra a planilha do RTD e tente de novo."
    )


def montar_instante(bruto, agora):
    """(instante, exato) - exato=False quando caiu no relogio do PC."""
    if bruto in (None, 0, "", "0"):
        return agora, False

    if isinstance(bruto, datetime):
        h, m, s = bruto.hour, bruto.minute, bruto.second
        if (h, m, s) == (0, 0, 0):
            return agora, False
        return agora.replace(hour=h, minute=m, second=s, microsecond=0), True

    txt = str(bruto).strip()
    if ":" in txt:
        partes = txt.split(" ")[-1].split(":")
        try:
            h, m = int(partes[0]), int(partes[1])
            s = int(float(partes[2])) if len(partes) > 2 else 0
        except (ValueError, IndexError):
            return agora, False
        if (h, m, s) == (0, 0, 0):
            return agora, False
        return agora.replace(hour=h, minute=m, second=s, microsecond=0), True

    try:
        total = round((float(txt) % 1) * 86400)
    except ValueError:
        return agora, False
    if total == 0:
        return agora, False
    return agora.replace(
        hour=total // 3600, minute=(total % 3600) // 60,
        second=total % 60, microsecond=0,
    ), True


def enviar(lote):
    if not lote:
        return True
    try:
        r = requests.post(
            URL_INGEST,
            headers={"X-Painel-Token": TOKEN, "Content-Type": "application/json"},
            json={"ticks": lote},
            timeout=20,
        )
        if r.status_code == 200:
            log.info(f"enviados {len(lote)} ticks -> {r.json()}")
            return True
        log.error(f"HTTP {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log.error(f"falha de rede: {e} - o lote sera reenviado")
    return False


def main():
    if not TOKEN:
        log.error('PAINEL_FRP0_TOKEN nao definido. Rode: setx PAINEL_FRP0_TOKEN "..."')
        log.error("Depois FECHE e reabra o terminal.")
        sys.exit(1)

    log.info("=" * 55)
    log.info(f"coletor FRP0 - janela {JANELA_INICIO} as {JANELA_FIM}")

    try:
        aba, linha, col_hora, col_valor = localizar()
    except Exception as e:
        log.error(str(e))
        sys.exit(1)

    hi, mi = map(int, JANELA_INICIO.split(":"))
    hf, mf = map(int, JANELA_FIM.split(":"))

    vistos = set()
    pendentes = []
    ultimo_envio = 0.0
    avisou_zero = False
    avisou_aprox = False

    while True:
        agora = datetime.now(BRT)
        minutos = agora.hour * 60 + agora.minute
        if minutos < hi * 60 + mi:
            time.sleep(5)
            continue
        if minutos > hf * 60 + mf:
            break

        try:
            valor = aba.Cells(linha, col_valor).Value
            hora = aba.Cells(linha, col_hora).Value if col_hora else None
        except Exception as e:
            log.error(f"leitura falhou: {e}")
            time.sleep(INTERVALO_SEG)
            continue

        # PROTECAO: RTD mudo devolve zero, nao erro
        if not isinstance(valor, (int, float)) or valor == 0:
            if not avisou_zero:
                log.warning(
                    "FRP0 veio zerado ou vazio - NADA sera enviado. "
                    "Verifique se o Profit esta aberto e se o Black Arrow "
                    "nao tomou o servidor RTD (abra o Profit primeiro)."
                )
                avisou_zero = True
            time.sleep(INTERVALO_SEG)
            continue

        if avisou_zero:
            log.info("FRP0 voltou a responder")
            avisou_zero = False

        instante, exato = montar_instante(hora, agora)
        if not exato and not avisou_aprox:
            log.warning(
                "sem hora valida do RTD - usando o relogio do PC. "
                "O casamento com a PTAX fica aproximado."
            )
            avisou_aprox = True

        chave = (instante.isoformat(), float(valor))
        if chave not in vistos:
            vistos.add(chave)
            pendentes.append({"instante": instante.isoformat(), "valor": float(valor)})
            log.info(f"{instante:%H:%M:%S}  FRP0 = {valor}")

        if pendentes and time.time() - ultimo_envio >= ENVIO_SEG:
            if enviar(pendentes):
                pendentes = []
            ultimo_envio = time.time()

        time.sleep(INTERVALO_SEG)

    enviar(pendentes)
    log.info(f"janela encerrada - {len(vistos)} leituras distintas")


if __name__ == "__main__":
    main()
