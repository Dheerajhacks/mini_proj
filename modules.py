from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.utils import secure_filename
from pymongo import MongoClient
import os
import pyttsx3
import base64
import whisper
import tempfile
import random
import google.generativeai as genai


app = Flask(__name__)
app.secret_key = 'your_secret_key'

client = MongoClient('mongodb://localhost:27017/')
db = client.dyslexia_assistance
progress_collection = db.progress
user_profile = db.user_profile
users_collection = db['users']
logs_collection = db['logs']

genai.configure(api_key='AIzaSyCfp6Cvn_VQXr-Tw-RRGMGB7RFUVokZ4ZE')

engine = pyttsx3.init()


def save_progress(user_id, module_id, reference_text, incorrect_words, text_done, audio_done):
    progress_data = {
        "user_id": user_id,
        "module_id": module_id,
        "text_done": text_done,
        "audio_done": audio_done,
        "reference_text": reference_text,
        "incorrect_words": incorrect_words,
    }
    progress_collection.insert_one(progress_data)

def get_all_progress(user_id):
    return list(progress_collection.find({"user_id": user_id}).sort("module_id", 1))


def get_latest_progress(user_id):
    return progress_collection.find_one({"user_id": user_id}, sort=[('_id', -1)])


def update_user_capability(user_id, reference_text, incorrect_words):
    total_words = len(reference_text.split())
    incorrect_count = len(incorrect_words)

    profile = user_profile.find_one({"user_id": user_id}) or {
        "user_id": user_id,
        "capability_score": 1.0,
        "history": {
            "total_attempts": 0,
            "total_words": 0,
            "total_errors": 0
        }
    }

    profile["history"]["total_attempts"] += 1
    profile["history"]["total_words"] += total_words
    profile["history"]["total_errors"] += incorrect_count

    total_words = profile["history"]["total_words"]
    total_errors = profile["history"]["total_errors"]
    profile["capability_score"] = max(0.1, round(1 - (total_errors / total_words), 2))

    user_profile.update_one({"user_id": user_id}, {"$set": profile}, upsert=True)


def generate_custom_paragraph(capability_score):
    if capability_score < 0.92:
        prompt = "Write a very simple English paragraph using short, easy words."
    elif capability_score < 0.89:
        prompt = "Generate a simple English paragraph using basic vocabulary and short sentences."
    else:
        prompt = "Generate an intermediate English paragraph with slightly challenging vocabulary. Keep it around 3-4 sentences."

    try:
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content([prompt])
        return response.text.strip()
    except Exception as e:
        print("Gemini API error:", e)
        return "The sun rises in the east and sets in the west."

@app.route('/module')
def module():
    user_id = session.get('user_id', 'guest')
    print(user_id)
    user = user_profile.find_one({'user_id': user_id}) or {"capability_score": 1.0}
    capability_score = user.get('capability_score', 1.0)
    paragraph = generate_custom_paragraph(capability_score)

    save_progress(user_id, module_id=0, reference_text=paragraph, incorrect_words=[], text_done=False, audio_done=False)

    return render_template('module.html', reference_text=paragraph, module_id=0)


@app.route('/')
def mycourse():
    return render_template('index.html')


@app.route('/generate_audio', methods=['POST'])
def generate_audio():
    data = request.get_json()
    rate = int(data.get('rate', 150))

    user_id = session.get('user_id', 'guest')
    progress = get_latest_progress(user_id)
    text = progress["reference_text"] if progress else "No text available"

    engine.setProperty('rate', rate)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_file:
        temp_path = temp_file.name
    engine.save_to_file(text, temp_path)
    engine.runAndWait()

    with open(temp_path, 'rb') as f:
        audio_base64 = base64.b64encode(f.read()).decode('utf-8')
    os.remove(temp_path)
    return jsonify({'audio': f'data:audio/wav;base64,{audio_base64}'})


def compare_texts(user_text, reference_text):
    ref_words = reference_text.split()
    user_words = user_text.split()
    incorrect_words = []

    for ref, user in zip(ref_words, user_words):
        if ref.lower() != user.lower():
            incorrect_words.append({'user': user, 'correct': ref})
    if len(user_words) < len(ref_words):
        for i in range(len(user_words), len(ref_words)):
            incorrect_words.append({'user': '', 'correct': ref_words[i]})
    elif len(user_words) > len(ref_words):
        for i in range(len(ref_words), len(user_words)):
            incorrect_words.append({'user': user_words[i], 'correct': ''})

    pronunciations = []
    for word in incorrect_words:
        if word['correct']:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_file:
                temp_path = temp_file.name
            engine.save_to_file(f"The correct word is {word['correct']}", temp_path)
            engine.runAndWait()
            with open(temp_path, 'rb') as f:
                audio_base64 = base64.b64encode(f.read()).decode('utf-8')
            os.remove(temp_path)
            pronunciations.append({
                'word': word['correct'],
                'audio': f'data:audio/wav;base64,{audio_base64}'
            })

    return incorrect_words, pronunciations


@app.route('/check_text', methods=['POST'])
def check_text():
    user_id = session.get('user_id', 'guest')
    user_text = request.json.get('text', '')

    progress = get_latest_progress(user_id)
    if not progress:
        return jsonify({'error': 'No reference text found'}), 400

    reference_text = progress["reference_text"]
    incorrect_words, pronunciations = compare_texts(user_text, reference_text)
    completed = len(incorrect_words) == 0

    save_progress(user_id, 0, reference_text, incorrect_words, text_done=completed, audio_done=progress['audio_done'])
    update_user_capability(user_id, reference_text, incorrect_words)

    return jsonify({
        'incorrect': incorrect_words,
        'pronunciations': pronunciations,
        'completed': completed,
        'points': 10 if completed else 0
    })


@app.route('/next_paragraph', methods=['GET'])
def next_paragraph():
    user_id = session.get('user_id', 'guest')
    user = user_profile.find_one({'user_id': user_id}) or {"capability_score": 1.0}
    capability_score = user.get('capability_score', 1.0)

    last_progress = get_latest_progress(user_id)
    last_module_id = last_progress['module_id'] if last_progress else 0
    next_module_id = last_module_id + 1

    paragraph = generate_custom_paragraph(capability_score)
    save_progress(user_id, module_id=next_module_id, reference_text=paragraph, incorrect_words=[], text_done=False, audio_done=False)

    return jsonify({'generated_paragraph': paragraph})

@app.route('/prev_paragraph', methods=['GET'])
def prev_paragraph():
    user_id = session.get('user_id', 'guest')
    all_progress = get_all_progress(user_id)

    if not all_progress or len(all_progress) < 2:
        return jsonify({'generated_paragraph': all_progress[0]['reference_text'] if all_progress else "No previous text available"})

    latest_module_id = all_progress[-1]['module_id']
    prev_progress = next((p for p in reversed(all_progress) if p['module_id'] < latest_module_id), None)

    if prev_progress:
        return jsonify({'generated_paragraph': prev_progress['reference_text']})
    else:
        return jsonify({'generated_paragraph': "No previous module found."})


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        if users_collection.find_one({"email": email}):
            return "User already exists."
        users_collection.insert_one({"email": email, "password": password})
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = users_collection.find_one({"email": email, "password": password})
        if user:
            session['user_id'] = user['email']
            return redirect(url_for('module'))
        return render_template('login.html', error="Invalid email or password.")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    print('logout....')
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(debug=True)
