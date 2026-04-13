from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, Response, flash, session)
import psycopg2
import psycopg2.extras
from datetime import datetime
import barcode
from barcode.writer import ImageWriter
import io, base64, csv, os, socket

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nlag_deposito_2026')

DATABASE_URL = os.environ.get('DATABASE_URL')

# ─────────────────────────────────────────
# UTILITÁRIOS DB
# ─────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def query(sql, params=(), fetchone=False, fetchall=False, commit=False):
    conn   = get_db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    result = None
    try:
        cur.execute(sql, params)
        if fetchone:
            result = cur.fetchone()
        elif fetchall:
            result = cur.fetchall()
        if commit:
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
    return result

def gerar_barcode_base64(codigo):
    try:
        barcode_class = barcode.get_barcode_class('code128')
        bc     = barcode_class(str(codigo), writer=ImageWriter())
        buffer = io.BytesIO()
        bc.write(buffer)
        return base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception:
        return None

def calcular_saldo(codigo):
    row = query('''
        SELECT
            COALESCE(SUM(CASE WHEN tipo='ENTRADA' THEN quantidade ELSE 0 END), 0)
          - COALESCE(SUM(CASE WHEN tipo='SAIDA'   THEN quantidade ELSE 0 END), 0)
          AS saldo
        FROM movimentacoes WHERE codigo = %s
    ''', (codigo,), fetchone=True)
    return float(row['saldo']) if row else 0.0

# ─────────────────────────────────────────
# IMPRESSÃO ZEBRA VIA REDE (ZPL)
# Sem forçar PW/LL — usa config da impressora
# ─────────────────────────────────────────
def gerar_zpl(codigo, descricao, data_hora, copias=1):
    desc  = descricao[:35] if len(descricao) > 35 else descricao
    desc2 = descricao[35:70] if len(descricao) > 35 else ''

    zpl  = "^XA\n"
    zpl += f"^PQ{copias},0,1,Y\n"
    zpl += "^FO20,15^A0N,28,28^FDNLAG - DEPOSITO^FS\n"
    zpl += "^FO20,48^GB700,2,2^FS\n"
    zpl += f"^FO20,60^A0N,24,24^FDCod: {codigo}^FS\n"

    if desc2:
        zpl += f"^FO20,90^A0N,22,22^FD{desc}^FS\n"
        zpl += f"^FO20,116^A0N,22,22^FD{desc2}^FS\n"
        zpl += f"^FO20,148^A0N,18,18^FDData: {data_hora}^FS\n"
        zpl += "^FO20,172^GB700,2,2^FS\n"
        zpl += f"^FO60,185^BCN,95,Y,N,N^FD{codigo}^FS\n"
    else:
        zpl += f"^FO20,90^A0N,22,22^FD{desc}^FS\n"
        zpl += f"^FO20,120^A0N,18,18^FDData: {data_hora}^FS\n"
        zpl += "^FO20,148^GB700,2,2^FS\n"
        zpl += f"^FO60,162^BCN,110,Y,N,N^FD{codigo}^FS\n"

    zpl += "^XZ"
    return zpl

def enviar_para_zebra(ip, zpl, porta=9100, timeout=5):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip.strip(), porta))
            s.sendall(zpl.encode('utf-8'))
        return True, "✅ Etiqueta(s) enviada(s) para a impressora com sucesso!"
    except socket.timeout:
        return False, f"❌ Timeout: impressora {ip} não respondeu. Verifique se está ligada na rede."
    except ConnectionRefusedError:
        return False, f"❌ Conexão recusada pelo IP {ip}. Verifique o IP e a porta 9100."
    except OSError as e:
        return False, f"❌ Erro de rede: {str(e)}"

# ─────────────────────────────────────────
# AUTENTICAÇÃO
# ─────────────────────────────────────────
USUARIO = os.environ.get('APP_USUARIO', 'nlag')
SENHA   = os.environ.get('APP_SENHA',   'deposito2026')

@app.before_request
def verificar_login():
    rotas_liberadas = ['login', 'static']
    if request.endpoint in rotas_liberadas:
        return
    if not session.get('logado'):
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        if (request.form['usuario'] == USUARIO and
                request.form['senha'] == SENHA):
            session['logado'] = True
            return redirect(url_for('index'))
        erro = "Usuário ou senha incorretos."
    return render_template('login.html', erro=erro)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────
@app.route('/')
def index():
    saldo = query('''
        SELECT
            m.codigo, m.descricao, m.unidade,
            COALESCE(SUM(CASE WHEN mov.tipo='ENTRADA' THEN mov.quantidade ELSE 0 END), 0)
          - COALESCE(SUM(CASE WHEN mov.tipo='SAIDA'   THEN mov.quantidade ELSE 0 END), 0)
          AS saldo
        FROM materiais m
        LEFT JOIN movimentacoes mov ON m.codigo = mov.codigo
        GROUP BY m.codigo, m.descricao, m.unidade
        ORDER BY m.descricao
    ''', fetchall=True) or []

    total_itens     = len(saldo)
    total_zerados   = sum(1 for s in saldo if float(s['saldo']) <= 0)
    total_com_saldo = total_itens - total_zerados

    return render_template('index.html',
                           saldo=saldo,
                           total_itens=total_itens,
                           total_zerados=total_zerados,
                           total_com_saldo=total_com_saldo,
                           agora=datetime.now().strftime('%d/%m/%Y %H:%M'))

# ─────────────────────────────────────────
# CADASTRO DE MATERIAIS
# ─────────────────────────────────────────
@app.route('/materiais', methods=['GET', 'POST'])
def materiais():
    if request.method == 'POST':
        acao = request.form.get('acao')

        if acao == 'cadastrar':
            codigo    = request.form['codigo'].strip().upper()
            descricao = request.form['descricao'].strip().upper()
            unidade   = request.form['unidade'].strip().upper()
            try:
                query(
                    'INSERT INTO materiais (codigo, descricao, unidade) VALUES (%s,%s,%s)',
                    (codigo, descricao, unidade), commit=True
                )
                flash(f"✅ Material {codigo} — {descricao} cadastrado!", "success")
            except Exception:
                flash(f"⚠️ Código {codigo} já existe no sistema.", "danger")

        elif acao == 'excluir':
            codigo = request.form['codigo'].strip().upper()
            query('DELETE FROM materiais WHERE codigo = %s',
                  (codigo,), commit=True)
            flash(f"🗑️ Material {codigo} excluído.", "warning")

    lista = query('SELECT * FROM materiais ORDER BY descricao',
                  fetchall=True) or []
    return render_template('materiais.html', lista=lista)

# ─────────────────────────────────────────
# IMPORTAR CSV
# ─────────────────────────────────────────
@app.route('/importar_csv', methods=['POST'])
def importar_csv():
    arquivo = request.files.get('arquivo_csv')
    if not arquivo:
        flash("Nenhum arquivo enviado.", "danger")
        return redirect(url_for('materiais'))

    raw      = arquivo.stream.read()
    conteudo = None
    for enc in ('utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1'):
        try:
            conteudo = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if conteudo is None:
        flash("❌ Não foi possível ler o arquivo.", "danger")
        return redirect(url_for('materiais'))

    stream  = io.StringIO(conteudo, newline=None)
    amostra = conteudo[:1024]
    sep     = ';' if amostra.count(';') >= amostra.count(',') else ','
    reader  = csv.DictReader(stream, delimiter=sep)

    inseridos = 0
    ignorados = 0
    erros     = []

    conn = get_db()

    for i, row in enumerate(reader, start=2):
        cur = conn.cursor()
        try:
            row       = {k.strip().lower().replace('\ufeff', ''): v
                         for k, v in row.items() if k}
            codigo    = row.get('codigo',    '').strip().upper()
            descricao = row.get('descricao', '').strip().upper()
            unidade   = row.get('unidade',   '').strip().upper()

            if not codigo or not descricao or not unidade:
                erros.append(f"Linha {i}: campos vazios")
                ignorados += 1
                cur.close()
                continue

            cur.execute(
                'INSERT INTO materiais (codigo, descricao, unidade) '
                'VALUES (%s, %s, %s) ON CONFLICT (codigo) DO NOTHING',
                (codigo, descricao, unidade)
            )
            if cur.rowcount > 0:
                inseridos += 1
            else:
                ignorados += 1
                erros.append(f"Linha {i}: '{codigo}' já existe")

            conn.commit()
            cur.close()

        except Exception as e:
            conn.rollback()
            erros.append(f"Linha {i}: erro — {str(e)[:60]}")
            ignorados += 1
            if not cur.closed:
                cur.close()

    conn.close()

    partes = [f"✅ {inseridos} material(is) importado(s)!"]
    if ignorados:
        partes.append(f"⚠️ {ignorados} linha(s) ignorada(s).")
    if erros:
        partes.append("Detalhes: " + " | ".join(erros[:5]))

    flash(" ".join(partes), "success" if inseridos > 0 else "warning")
    return redirect(url_for('materiais'))

# ─────────────────────────────────────────
# ENTRADA + IMPRESSÃO ZEBRA
# ─────────────────────────────────────────
@app.route('/entrada', methods=['GET', 'POST'])
def entrada():
    material    = None
    barcode_img = None
    quantidade  = None
    agora_str   = datetime.now().strftime('%d/%m/%Y %H:%M')
    msg_zebra   = None

    if request.method == 'POST':
        acao      = request.form.get('acao', 'registrar')
        codigo    = request.form.get('codigo', '').strip().upper()
        quantidade = request.form.get('quantidade', '')
        obs       = request.form.get('observacao', '')
        data_hora = datetime.now().strftime('%d/%m/%Y %H:%M')

        material = query(
            'SELECT * FROM materiais WHERE codigo = %s',
            (codigo,), fetchone=True
        )

        if acao == 'registrar':
            if material:
                try:
                    qtd = float(quantidade)
                    if qtd <= 0:
                        flash("❌ Quantidade deve ser maior que zero.", "danger")
                    else:
                        query(
                            'INSERT INTO movimentacoes '
                            '(codigo, tipo, quantidade, data_hora, observacao) '
                            'VALUES (%s,%s,%s,%s,%s)',
                            (codigo, 'ENTRADA', qtd,
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'), obs),
                            commit=True
                        )
                        barcode_img = gerar_barcode_base64(codigo)
                        flash(f"✅ Entrada de {qtd} {material['unidade']} "
                              f"de {material['descricao']} registrada!", "success")
                except ValueError:
                    flash("❌ Quantidade inválida.", "danger")
            else:
                flash(f"❌ Código {codigo} não encontrado.", "danger")

        elif acao == 'imprimir_zebra':
            ip_zebra = request.form.get('ip_zebra', '').strip()
            copias   = request.form.get('copias', '1').strip()
            try:
                copias_int = max(1, min(999, int(copias)))
            except ValueError:
                copias_int = 1

            if not ip_zebra:
                msg_zebra = ("danger", "❌ Informe o IP da impressora Zebra.")
            elif not material:
                msg_zebra = ("danger", f"❌ Código {codigo} não encontrado.")
            else:
                zpl = gerar_zpl(codigo, material['descricao'], data_hora, copias_int)
                ok, mensagem = enviar_para_zebra(ip_zebra, zpl)
                msg_zebra = ("success" if ok else "danger", mensagem)
                barcode_img = gerar_barcode_base64(codigo)
                session['ip_zebra'] = ip_zebra

    materiais_lista = query(
        'SELECT codigo, descricao FROM materiais ORDER BY descricao',
        fetchall=True
    ) or []

    return render_template('entrada.html',
                           material=material,
                           barcode_img=barcode_img,
                           materiais=materiais_lista,
                           quantidade=quantidade,
                           agora=agora_str,
                           msg_zebra=msg_zebra,
                           ip_zebra_salvo=session.get('ip_zebra', ''))

# ─────────────────────────────────────────
# IMPRIMIR ETIQUETA AVULSA
# ─────────────────────────────────────────
@app.route('/imprimir_etiqueta', methods=['GET', 'POST'])
def imprimir_etiqueta():
    material    = None
    barcode_img = None
    msg_zebra   = None
    agora_str   = datetime.now().strftime('%d/%m/%Y %H:%M')

    if request.method == 'POST':
        codigo   = request.form.get('codigo', '').strip().upper()
        ip_zebra = request.form.get('ip_zebra', '').strip()
        copias   = request.form.get('copias', '1').strip()

        try:
            copias_int = max(1, min(999, int(copias)))
        except ValueError:
            copias_int = 1

        material = query(
            'SELECT * FROM materiais WHERE codigo = %s',
            (codigo,), fetchone=True
        )

        if not material:
            flash(f"❌ Código {codigo} não encontrado.", "danger")
        elif not ip_zebra:
            flash("❌ Informe o IP da impressora Zebra.", "danger")
            barcode_img = gerar_barcode_base64(codigo)
        else:
            zpl = gerar_zpl(codigo, material['descricao'], agora_str, copias_int)
            ok, mensagem = enviar_para_zebra(ip_zebra, zpl)
            msg_zebra = ("success" if ok else "danger", mensagem)
            barcode_img = gerar_barcode_base64(codigo)
            session['ip_zebra'] = ip_zebra

    materiais_lista = query(
        'SELECT codigo, descricao FROM materiais ORDER BY descricao',
        fetchall=True
    ) or []

    return render_template('imprimir_etiqueta.html',
                           material=material,
                           barcode_img=barcode_img,
                           materiais=materiais_lista,
                           msg_zebra=msg_zebra,
                           ip_zebra_salvo=session.get('ip_zebra', ''),
                           agora=agora_str)

# ─────────────────────────────────────────
# SAÍDA
# ─────────────────────────────────────────
@app.route('/saida', methods=['GET', 'POST'])
def saida():
    if request.method == 'POST':
        codigo    = request.form.get('codigo', '').strip().upper()
        obs       = request.form.get('observacao', '')
        data_hora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            quantidade = float(request.form.get('quantidade', 0))
        except ValueError:
            quantidade = 0

        material = query(
            'SELECT * FROM materiais WHERE codigo = %s',
            (codigo,), fetchone=True
        )

        if material:
            saldo_atual = calcular_saldo(codigo)
            if quantidade <= 0:
                flash("❌ Quantidade deve ser maior que zero.", "danger")
            elif quantidade > saldo_atual:
                flash(f"❌ Saldo insuficiente! Saldo atual: "
                      f"{int(saldo_atual) if saldo_atual == int(saldo_atual) else saldo_atual}"
                      f" {material['unidade']}", "danger")
            else:
                query(
                    'INSERT INTO movimentacoes '
                    '(codigo, tipo, quantidade, data_hora, observacao) '
                    'VALUES (%s,%s,%s,%s,%s)',
                    (codigo, 'SAIDA', quantidade, data_hora, obs), commit=True
                )
                flash(f"✅ Saída de "
                      f"{int(quantidade) if quantidade == int(quantidade) else quantidade}"
                      f" {material['unidade']} de {material['descricao']} registrada!", "success")
        else:
            flash(f"❌ Código {codigo} não encontrado.", "danger")

    return render_template('saida.html')

# ─────────────────────────────────────────
# HISTÓRICO
# ─────────────────────────────────────────
@app.route('/historico')
def historico():
    filtro = request.args.get('codigo', '').strip().upper()
    tipo_f = request.args.get('tipo', '')

    sql    = '''
        SELECT mov.*, m.descricao, m.unidade
        FROM movimentacoes mov
        JOIN materiais m ON mov.codigo = m.codigo
        WHERE 1=1
    '''
    params = []
    if filtro:
        sql += ' AND mov.codigo LIKE %s'
        params.append(f'%{filtro}%')
    if tipo_f in ('ENTRADA', 'SAIDA'):
        sql += ' AND mov.tipo = %s'
        params.append(tipo_f)
    sql += ' ORDER BY mov.data_hora DESC LIMIT 500'

    movs = query(sql, params, fetchall=True) or []
    return render_template('historico.html',
                           movs=movs, filtro=filtro, tipo_f=tipo_f)

# ─────────────────────────────────────────
# EXPORTAR SALDO CSV
# ─────────────────────────────────────────
@app.route('/exportar_saldo')
def exportar_saldo():
    saldo = query('''
        SELECT
            m.codigo, m.descricao, m.unidade,
            COALESCE(SUM(CASE WHEN mov.tipo='ENTRADA' THEN mov.quantidade ELSE 0 END), 0)
          - COALESCE(SUM(CASE WHEN mov.tipo='SAIDA'   THEN mov.quantidade ELSE 0 END), 0)
          AS saldo
        FROM materiais m
        LEFT JOIN movimentacoes mov ON m.codigo = mov.codigo
        GROUP BY m.codigo, m.descricao, m.unidade
        ORDER BY m.descricao
    ''', fetchall=True) or []

    def gerar():
        yield 'Codigo;Descricao;Unidade;Saldo\n'
        for row in saldo:
            s  = float(row['saldo'])
            sf = str(int(s)) if s == int(s) else f"{s:.2f}"
            yield f"{row['codigo']};{row['descricao']};{row['unidade']};{sf}\n"

    return Response(gerar(), mimetype='text/csv',
                    headers={'Content-Disposition':
                             'attachment; filename=saldo_estoque.csv'})

# ─────────────────────────────────────────
# EXPORTAR HISTÓRICO CSV
# ─────────────────────────────────────────
@app.route('/exportar_historico')
def exportar_historico():
    movs = query('''
        SELECT mov.data_hora, mov.tipo, mov.codigo, m.descricao,
               m.unidade, mov.quantidade, mov.observacao
        FROM movimentacoes mov
        JOIN materiais m ON mov.codigo = m.codigo
        ORDER BY mov.data_hora DESC
    ''', fetchall=True) or []

    def gerar():
        yield 'Data/Hora;Tipo;Codigo;Descricao;Unidade;Quantidade;Observacao\n'
        for r in movs:
            yield (f"{r['data_hora']};{r['tipo']};{r['codigo']};"
                   f"{r['descricao']};{r['unidade']};{r['quantidade']};"
                   f"{r['observacao'] or ''}\n")

    return Response(gerar(), mimetype='text/csv',
                    headers={'Content-Disposition':
                             'attachment; filename=historico.csv'})

# ─────────────────────────────────────────
# API AJAX
# ─────────────────────────────────────────
@app.route('/api/material/<codigo>')
def api_material(codigo):
    material = query(
        'SELECT * FROM materiais WHERE codigo = %s',
        (codigo.upper(),), fetchone=True
    )
    saldo = calcular_saldo(codigo.upper()) if material else 0.0
    if material:
        dados          = dict(material)
        dados['saldo'] = saldo
        return jsonify(dados)
    return jsonify({'erro': 'não encontrado'}), 404

# ─────────────────────────────────────────
# MODO COLETOR
# ─────────────────────────────────────────
@app.route('/coletor')
def coletor():
    return render_template('coletor.html')

# ─────────────────────────────────────────
# INICIALIZAÇÃO
# ─────────────────────────────────────────
if __name__ == '__main__':
    from database import init_db
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)
