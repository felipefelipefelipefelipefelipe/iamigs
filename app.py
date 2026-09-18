import os
import re
import sqlite3
from datetime import date, datetime

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'iamigs-dev-secret-change-me')
DB = os.path.join(os.path.dirname(__file__), 'iamigs.db')

CHALLENGES = [
    ('Quebre o gelo', 'Dê bom dia e puxe uma conversa curta com alguém da sua turma.', 10),
    ('Novo rosto', 'Converse por alguns minutos com uma pessoa que você ainda não conhece bem.', 10),
    ('Pequeno gesto', 'Ajude alguém da escola com uma tarefa simples hoje.', 10),
    ('Explore', 'Descubra um lugar da escola que você ainda não conhecia.', 10),
    ('Pergunte', 'Pergunte a um colega qual matéria ele mais gosta e por quê.', 10),
    ('Compartilhe', 'Conte para alguém uma coisa que você curte e descubra um interesse em comum.', 10),
    ('Elogio sincero', 'Faça um elogio respeitoso e sincero para alguém.', 10),
]


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            points INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS completed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            challenge TEXT,
            day TEXT,
            UNIQUE(user_id, day)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chat_user_id
        ON chat_messages(user_id, id);
    ''')
    conn.commit()
    conn.close()


init_db()


def current_user():
    uid = session.get('user_id')
    if not uid:
        return None

    conn = db()
    user = conn.execute(
        'SELECT * FROM users WHERE id = ?',
        (uid,)
    ).fetchone()
    conn.close()
    return user


def challenge_today():
    idx = date.today().toordinal() % len(CHALLENGES)
    return CHALLENGES[idx]


def save_chat_message(user_id, role, content):
    conn = db()
    conn.execute(
        'INSERT INTO chat_messages(user_id, role, content, created_at) VALUES (?, ?, ?, ?)',
        (user_id, role, content, datetime.now().isoformat(timespec='seconds'))
    )
    conn.commit()
    conn.close()


def get_chat_history(user_id, limit=12):
    """Pega as últimas mensagens para a IA manter contexto."""
    conn = db()
    rows = conn.execute(
        '''SELECT role, content
           FROM chat_messages
           WHERE user_id = ?
           ORDER BY id DESC
           LIMIT ?''',
        (user_id, limit)
    ).fetchall()
    conn.close()

    # O banco traz do mais novo para o mais antigo; a API precisa da ordem normal.
    rows.reverse()
    return [{'role': row['role'], 'content': row['content']} for row in rows]


def clear_chat_history(user_id):
    conn = db()
    conn.execute('DELETE FROM chat_messages WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def clean_ai_reply(reply):
    """Evita respostas gigantescas/listas mesmo se o modelo ignorar o prompt."""
    if not reply:
        return 'Foi mal 😅 não consegui pensar em uma resposta agora.'

    reply = reply.strip()

    # Remove alguns formatos de cabeçalho que deixam a conversa com cara de texto escolar.
    reply = re.sub(r'^\s*#{1,6}\s*', '', reply)

    # Limite extra de segurança para o tamanho da resposta exibida.
    if len(reply) > 900:
        reply = reply[:900].rsplit(' ', 1)[0].rstrip() + '...'

    return reply


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:40]
        if name:
            conn = db()
            cur = conn.execute('INSERT INTO users(name) VALUES (?)', (name,))
            conn.commit()
            uid = cur.lastrowid
            conn.close()

            session['user_id'] = uid
            return redirect(url_for('dashboard'))

    if current_user():
        return redirect(url_for('dashboard'))

    return render_template('index.html')


@app.route('/dashboard')
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for('index'))

    challenge = challenge_today()
    conn = db()

    done = conn.execute(
        'SELECT 1 FROM completed WHERE user_id = ? AND day = ?',
        (user['id'], str(date.today()))
    ).fetchone()

    total = conn.execute(
        'SELECT COUNT(*) n FROM completed WHERE user_id = ?',
        (user['id'],)
    ).fetchone()['n']

    conn.close()

    return render_template(
        'dashboard.html',
        user=user,
        challenge=challenge,
        done=bool(done),
        total=total
    )


@app.route('/chat')
def chat():
    user = current_user()
    if not user:
        return redirect(url_for('index'))

    return render_template('chat.html', user=user)


@app.route('/desafios')
def desafios():
    user = current_user()
    if not user:
        return redirect(url_for('index'))

    conn = db()
    rows = conn.execute(
        'SELECT day, challenge FROM completed WHERE user_id = ? ORDER BY id DESC',
        (user['id'],)
    ).fetchall()
    conn.close()

    return render_template(
        'desafios.html',
        user=user,
        challenge=challenge_today(),
        completed=rows
    )


@app.post('/api/challenge/complete')
def complete_challenge():
    user = current_user()
    if not user:
        return jsonify(error='Não autenticado'), 401

    challenge = challenge_today()
    conn = db()

    try:
        conn.execute(
            'INSERT INTO completed(user_id, challenge, day) VALUES (?, ?, ?)',
            (user['id'], challenge[0], str(date.today()))
        )
        conn.execute(
            'UPDATE users SET points = points + ? WHERE id = ?',
            (challenge[2], user['id'])
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False

    updated_user = conn.execute(
        'SELECT * FROM users WHERE id = ?',
        (user['id'],)
    ).fetchone()
    conn.close()

    return jsonify(ok=ok, points=updated_user['points'])


@app.get('/api/chat/history')
def chat_history():
    """Retorna o histórico para a interface, se o front quiser restaurá-lo."""
    user = current_user()
    if not user:
        return jsonify(error='Não autenticado'), 401

    return jsonify(messages=get_chat_history(user['id'], limit=30))


@app.post('/api/chat/clear')
def chat_clear():
    user = current_user()
    if not user:
        return jsonify(error='Não autenticado'), 401

    clear_chat_history(user['id'])
    return jsonify(ok=True)


@app.post('/api/chat')
def api_chat():
    user = current_user()
    if not user:
        return jsonify(error='Não autenticado'), 401

    data = request.get_json(silent=True) or {}
    message = str(data.get('message', '')).strip()

    if not message:
        return jsonify(error='Mensagem vazia'), 400

    # Evita mandar mensagens absurdamente grandes para a API.
    message = message[:2000]

    api_key = os.getenv('GROQ_API_KEY')
    if not api_key:
        return jsonify(
            reply='A IA ainda não foi configurada. Coloque sua GROQ_API_KEY no arquivo .env e reinicie o site.'
        )

    # Salva a mensagem antes de chamar a IA para manter o histórico.
    save_chat_message(user['id'], 'user', message)

    try:
        from groq import Groq

        client = Groq(api_key=api_key)

        system_prompt = f'''
Você é o IAmigs, um assistente para estudantes que estão se adaptando à escola.
O usuário se chama {user['name']}.

PERSONALIDADE:
- Português brasileiro.
- Natural, jovem, acolhedor e direto.
- Converse como uma pessoa, não como um professor, psicólogo ou texto de trabalho.
- Você não é terapeuta.
- Pode usar humor leve quando o assunto permitir.
- Nunca use humor em situações sérias.

ESTILO:
- Normalmente responda em 1 a 3 frases curtas.
- Seja direto ao ponto.
- Nunca use listas, tópicos, títulos ou subtítulos sem o usuário pedir.
- Não faça textões.
- Não repita o que o usuário acabou de dizer.
- Não use frases motivacionais genéricas.
- Não use o nome do usuário sem necessidade.
- Use no máximo 1 emoji, e somente quando combinar.
- Não faça pergunta no final automaticamente.
- Só faça uma pergunta se ela realmente ajudar a continuar a conversa.
- Não exagere nas gírias.

CONTEXTO:
- Esta é uma conversa contínua.
- Use as mensagens anteriores para entender respostas curtas, brincadeiras, mudanças de assunto e referências.
- Nunca invente o significado de uma mensagem.
- Se uma mensagem curta for ambígua, pergunte de forma curta.
- Responda principalmente à última mensagem do usuário.
- Não reinicie a conversa a cada mensagem.

COMPORTAMENTO:
- Se o usuário estiver irritado ou xingando, não diga automaticamente "calma".
- Reconheça o que aconteceu e responda naturalmente.
- Para problemas escolares, dê sugestões práticas e curtas.
- Não invente regras ou informações da escola.

SEGURANÇA:
- Se o usuário falar sobre querer morrer, se machucar ou não estar seguro, leve a situação a sério.
- Responda de forma curta, acolhedora e direta.
- Incentive procurar imediatamente um adulto de confiança que esteja por perto.
- Se houver perigo imediato, incentive buscar ajuda de emergência com um adulto responsável.
- Não dê instruções sobre se machucar, esconder sinais ou fazer algo perigoso.
'''

        history = get_chat_history(user['id'], limit=12)
        messages = [
            {'role': 'system', 'content': system_prompt},
            *history
        ]

        response = client.chat.completions.create(
            model='openai/gpt-oss-120b',
            messages=messages,
            temperature=0.55,
            max_tokens=300
        )

        reply = clean_ai_reply(response.choices[0].message.content)
        save_chat_message(user['id'], 'assistant', reply)

        return jsonify(reply=reply)

    except Exception as e:
        print('Groq error:', repr(e))
        return jsonify(
            reply='Não consegui falar com a IA agora 😕 Confira sua chave da Groq e a conexão com a internet.'
        ), 200


@app.get('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True)