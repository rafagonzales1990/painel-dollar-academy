"""
diagnostico_excel.py - descobre por que o Python nao acha o Excel
================================================================
Rode com o Excel ABERTO e a planilha do RTD na tela:

    cd C:\\dev\\painel-dollar-academy
    python diagnostico_excel.py

Manda a saida inteira. Ela diz em qual etapa a conexao quebra.
================================================================
"""

import sys
import subprocess

print("=" * 60)
print("1. PROCESSOS DO EXCEL")
print("=" * 60)
try:
    saida = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    print(saida.strip())
except Exception as e:
    print(f"falhou: {e}")

print()
print("=" * 60)
print("2. BIBLIOTECAS")
print("=" * 60)
print(f"python: {sys.version.split()[0]}  ({sys.executable})")
try:
    import win32com.client
    import pythoncom
    import win32api
    print(f"pywin32: instalado")
except ImportError as e:
    print(f"pywin32 AUSENTE: {e}")
    print("rode: pip install pywin32")
    sys.exit(1)

print()
print("=" * 60)
print("3. GetActiveObject")
print("=" * 60)
pythoncom.CoInitialize()
excel = None
try:
    excel = win32com.client.GetActiveObject("Excel.Application")
    print(f"OK - versao {excel.Version}, {excel.Workbooks.Count} planilhas")
    for i in range(excel.Workbooks.Count):
        wb = excel.Workbooks.Item(i + 1)
        print(f"   [{i+1}] {wb.Name}  ({wb.Worksheets.Count} abas)")
except Exception as e:
    print(f"FALHOU: {e}")

print()
print("=" * 60)
print("4. RUNNING OBJECT TABLE (lista de objetos abertos)")
print("=" * 60)
try:
    ctx = pythoncom.CreateBindCtx(0)
    rot = pythoncom.GetRunningObjectTable()
    enum = rot.EnumRunning()
    total = 0
    planilhas = 0
    for moniker in enum:
        total += 1
        try:
            nome = moniker.GetDisplayName(ctx, None)
        except Exception:
            continue
        if nome.lower().endswith((".xlsx", ".xlsm", ".xls", ".xlsb")):
            planilhas += 1
            print(f"   PLANILHA: {nome}")
        elif total <= 25:
            print(f"   (outro)   {nome[:90]}")
    print(f"\n   total de objetos na ROT: {total}")
    print(f"   planilhas encontradas:   {planilhas}")
except Exception as e:
    print(f"FALHOU: {type(e).__name__}: {e}")

print()
print("=" * 60)
print("5. Dispatch (ultimo recurso)")
print("=" * 60)
try:
    app = win32com.client.Dispatch("Excel.Application")
    print(f"OK - versao {app.Version}, {app.Workbooks.Count} planilhas")
    for i in range(app.Workbooks.Count):
        print(f"   [{i+1}] {app.Workbooks.Item(i + 1).Name}")
    if app.Workbooks.Count == 0:
        print("   (instancia vazia: o Dispatch criou um Excel novo,")
        print("    separado do que esta na sua tela)")
except Exception as e:
    print(f"FALHOU: {e}")

print()
print("fim do diagnostico")
