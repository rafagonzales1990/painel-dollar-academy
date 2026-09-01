Attribute VB_Name = "ColetorFRP0"
' ============================================================
' ColetorFRP0 - Painel Dollar Academy
' ============================================================
' Le o FRP0 do RTD e envia para a Edge Function ingest-frp0.
' Roda dentro do proprio Excel: nao precisa de Python nem de
' terminal aberto.
'
' COMO USAR
'   IniciarColeta  - comeca a enviar (a cada 15 segundos)
'   PararColeta    - para
'   TestarUmEnvio  - manda uma leitura agora, para testar
'
' O token vem da variavel de ambiente PAINEL_FRP0_TOKEN.
' ============================================================

Option Explicit

Private Const URL_INGEST As String = _
    "https://uoabiezsuezmhwtbeerm.supabase.co/functions/v1/ingest-frp0"

Private Const ATIVO As String = "FRP0"
Private Const INTERVALO_SEG As Long = 15

' janela de coleta (formato 24h)
Private Const HORA_INICIO As String = "09:55"
Private Const HORA_FIM As String = "10:35"

Private proximaExecucao As Date
Private coletando As Boolean
Private ultimoValor As Double
Private ultimaHora As String


' ------------------------------------------------------------
' controle
' ------------------------------------------------------------

Public Sub IniciarColeta()
    coletando = True
    ultimoValor = -1
    ultimaHora = ""
    Application.StatusBar = "Coletor FRP0: ativo"
    Debug.Print "--- coletor iniciado " & Now
    Coletar
End Sub

Public Sub PararColeta()
    coletando = False
    On Error Resume Next
    Application.OnTime proximaExecucao, "ColetorFRP0.Coletar", , False
    On Error GoTo 0
    Application.StatusBar = False
    Debug.Print "--- coletor parado " & Now
End Sub

Public Sub TestarUmEnvio()
    Dim r As String
    r = LerEEnviar(True)
    MsgBox r, vbInformation, "Teste do coletor FRP0"
End Sub


' ------------------------------------------------------------
' laco agendado
' ------------------------------------------------------------

Public Sub Coletar()
    If Not coletando Then Exit Sub

    Dim agora As Date
    agora = Now

    Dim dentro As Boolean
    dentro = (TimeValue(agora) >= TimeValue(HORA_INICIO)) And _
             (TimeValue(agora) <= TimeValue(HORA_FIM))

    If dentro Then
        LerEEnviar False
        Application.StatusBar = "Coletor FRP0: ativo - ultimo envio " & _
                                Format(Now, "hh:nn:ss")
    Else
        Application.StatusBar = "Coletor FRP0: aguardando a janela " & _
                                HORA_INICIO & "-" & HORA_FIM
    End If

    proximaExecucao = DateAdd("s", INTERVALO_SEG, Now)
    Application.OnTime proximaExecucao, "ColetorFRP0.Coletar"
End Sub


' ------------------------------------------------------------
' leitura e envio
' ------------------------------------------------------------

Private Function LerEEnviar(forcar As Boolean) As String
    Dim ws As Worksheet, linha As Long
    Dim colHora As Long, colValor As Long
    Dim valor As Double, horaTxt As String

    If Not Localizar(ws, linha, colHora, colValor) Then
        LerEEnviar = "nao achei a linha '" & ATIVO & "' em nenhuma aba"
        Debug.Print LerEEnviar
        Exit Function
    End If

    Dim bruto As Variant
    bruto = ws.Cells(linha, colValor).Value

    If Not IsNumeric(bruto) Then
        LerEEnviar = "FRP0 nao numerico - nada enviado"
        Debug.Print LerEEnviar
        Exit Function
    End If

    valor = CDbl(bruto)

    ' PROTECAO: RTD mudo devolve zero, nao erro
    If valor = 0 Then
        LerEEnviar = "FRP0 zerado - o RTD nao esta respondendo. " & _
                     "Abra o Profit antes do Black Arrow."
        Debug.Print LerEEnviar
        Exit Function
    End If

    If colHora > 0 Then
        horaTxt = MontarHora(ws.Cells(linha, colHora).Value)
    Else
        horaTxt = Format(Now, "hh:nn:ss")
    End If

    ' nada mudou desde a ultima leitura
    If Not forcar Then
        If valor = ultimoValor And horaTxt = ultimaHora Then
            LerEEnviar = "sem negocio novo"
            Exit Function
        End If
    End If

    ultimoValor = valor
    ultimaHora = horaTxt

    Dim instante As String
    instante = Format(Date, "yyyy-mm-dd") & "T" & horaTxt & "-03:00"

    Dim corpo As String
    corpo = "{""ticks"":[{""instante"":""" & instante & """,""valor"":" & _
            Replace(CStr(valor), ",", ".") & "}]}"

    LerEEnviar = Enviar(corpo) & "  |  " & instante & "  FRP0=" & valor
    Debug.Print LerEEnviar
End Function


Private Function Enviar(corpo As String) As String
    Dim token As String
    token = Environ("PAINEL_FRP0_TOKEN")
    If token = "" Then
        Enviar = "ERRO: variavel PAINEL_FRP0_TOKEN nao encontrada. " & _
                 "Rode setx no PowerShell e REINICIE o Excel."
        Exit Function
    End If

    Dim http As Object
    On Error GoTo falha
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 5000, 5000, 10000, 15000
    http.Open "POST", URL_INGEST, False
    http.setRequestHeader "Content-Type", "application/json"
    http.setRequestHeader "X-Painel-Token", token
    http.send corpo

    Enviar = "HTTP " & http.Status & ": " & Left(http.responseText, 160)
    Exit Function

falha:
    Enviar = "ERRO de rede: " & Err.Description
End Function


' ------------------------------------------------------------
' apoio
' ------------------------------------------------------------

' Procura a linha do FRP0 e as colunas Hora / Ultimo pelo cabecalho.
Private Function Localizar(ByRef wsOut As Worksheet, ByRef linhaOut As Long, _
                           ByRef colHoraOut As Long, _
                           ByRef colValorOut As Long) As Boolean
    Dim ws As Worksheet, r As Long, c As Long
    Dim linhaAtivo As Long

    For Each ws In ThisWorkbook.Worksheets
        linhaAtivo = 0
        For r = 1 To 100
            If Trim(LCase(CStr(ws.Cells(r, 1).Value))) = LCase(ATIVO) Then
                linhaAtivo = r
                Exit For
            End If
        Next r
        If linhaAtivo > 0 Then
            colHoraOut = 0
            colValorOut = 0
            For r = 1 To linhaAtivo - 1
                For c = 1 To 40
                    Select Case Normalizar(ws.Cells(r, c).Value)
                        Case "hora": colHoraOut = c
                        Case "ultimo": colValorOut = c
                    End Select
                Next c
                If colValorOut > 0 Then Exit For
            Next r
            If colValorOut > 0 Then
                Set wsOut = ws
                linhaOut = linhaAtivo
                Localizar = True
                Exit Function
            End If
        End If
    Next ws

    Localizar = False
End Function


Private Function Normalizar(v As Variant) As String
    Dim t As String
    t = Trim(LCase(CStr(v)))
    t = Replace(t, ChrW(250), "u")   ' u acentuado
    t = Replace(t, ChrW(225), "a")
    t = Replace(t, ChrW(233), "e")
    t = Replace(t, ChrW(237), "i")
    t = Replace(t, ChrW(243), "o")
    t = Replace(t, ChrW(231), "c")
    Normalizar = t
End Function


' Converte o que o RTD devolveu em "hh:nn:ss".
Private Function MontarHora(v As Variant) As String
    If IsEmpty(v) Or IsNull(v) Then
        MontarHora = Format(Now, "hh:nn:ss")
        Exit Function
    End If

    If IsNumeric(v) Then
        If CDbl(v) = 0 Then
            MontarHora = Format(Now, "hh:nn:ss")
        Else
            MontarHora = Format(CDate(v), "hh:nn:ss")
        End If
        Exit Function
    End If

    On Error GoTo semHora
    MontarHora = Format(TimeValue(CStr(v)), "hh:nn:ss")
    Exit Function

semHora:
    MontarHora = Format(Now, "hh:nn:ss")
End Function
