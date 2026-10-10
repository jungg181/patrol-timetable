"""Equipment Patrol Timetable - standalone Flask app (public, no login).

Dashboard for monitors: current patrols based on real time, ON PATROL NOW,
QR clock-in/out for workers' phones, full timetable, settings, timecards.
"""
import os
import io
import json
import uuid
import csv
from datetime import datetime, date
from zoneinfo import ZoneInfo

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, send_file, abort, flash)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data.json')
# Factory local time (user's timezone). All "now" logic uses this.
TZ = ZoneInfo("America/Phoenix")

app = Flask(__name__)
app.secret_key = 'patrol-board-local-key'

COLLECTIONS = ['slots', 'equipment', 'shifts', 'workers', 'punches']


def new_id():
    return uuid.uuid4().hex[:8]


def now_iso():
    return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec='seconds')


def local_now():
    return datetime.now(TZ).replace(tzinfo=None)


def seed():
    slot_labels = (["7:00", "8:00", "9:00", "10:00", "11:00", "12:00"] +
                   ["13:00", "14:00", "15:00", "16:00", "17:00", "18:00",
                    "19:00", "20:00"] +
                   ["21:00", "22:00", "23:00", "0:00", "1:00", "2:00",
                    "3:00", "4:00"] +
                   ["5:00", "6:00"])
    slot_shifts = ["A"] * 6 + ["B"] * 8 + ["C"] * 8 + ["A"] * 2
    slots = [{"label": l, "shift": s} for l, s in zip(slot_labels, slot_shifts)]

    def eq(name, checks, active=True, note=""):
        return {"id": new_id(), "name": name, "note": note,
                "active": active,
                "checks": {str(k): v for k, v in checks.items()}}

    equipment = [
        eq("HOB", {0: 1, 2: 2, 4: 1, 6: 1, 8: 1, 10: 2, 12: 1, 14: 1,
                   16: 1, 18: 2, 20: 1, 22: 1}),
        eq("HOB LEAK CHECK", {0: 1, 4: 1, 8: 1, 12: 1, 16: 1, 20: 1}),
        eq("SOLVENT RECOVERY", {1: 2, 5: 1, 9: 2, 13: 1, 17: 2, 21: 1}),
        eq("SRP RO", {3: 1, 7: 2, 11: 1, 15: 2, 19: 1, 23: 2}),
        eq("SUPPLY & EXHAUST FAN", {2: 1, 10: 1, 18: 1}),
        eq("SOLVENT PURIFICATION", {1: 1, 5: 1, 9: 1, 13: 1, 17: 1, 21: 1},
           active=False, note="Scheduled for November"),
        eq("WASTE NMP RECOVERY", {1: 1, 5: 1, 9: 1, 13: 1, 17: 1, 21: 1},
           active=False, note="Scheduled for November"),
        eq("COOLING TOWER", {3: 1, 7: 1, 11: 1, 15: 1, 19: 1, 23: 1},
           active=False, note="Scheduled for November"),
    ]
    shifts = [
        {"id": "A", "name": "A", "color": "#2563eb",
         "intervals": [{"start": "07:00", "end": "12:00"},
                       {"start": "05:00", "end": "06:00"}]},
        {"id": "B", "name": "B", "color": "#059669",
         "intervals": [{"start": "13:00", "end": "20:00"}]},
        {"id": "C", "name": "C", "color": "#7c3aed",
         "intervals": [{"start": "21:00", "end": "04:00"}]},
    ]
    return {"slots": slots, "equipment": equipment, "shifts": shifts,
            "workers": [], "punches": []}


def load_db():
    if not os.path.exists(DB_PATH):
        db = seed()
        with open(DB_PATH, 'w') as f:
            json.dump(db, f, indent=2)
        return db
    with open(DB_PATH) as f:
        db = json.load(f)
    for c in COLLECTIONS:
        db.setdefault(c, [])
    return db


def save_db():
    with open(DB_PATH, 'w') as f:
        json.dump(DB, f, indent=2)


DB = load_db()


def find(coll, _id):
    return next((r for r in DB[coll] if r.get('id') == _id), None)


def slot_hour(label):
    try:
        return int(label.split(':')[0])
    except (ValueError, IndexError):
        return None


def current_slot_index():
    h = local_now().hour
    for i, s in enumerate(DB['slots']):
        if slot_hour(s['label']) == h:
            return i
    return 0


def shift_for_hour(h):
    def to_min(t):
        hh, mm = t.split(':')
        return int(hh) * 60 + int(mm)
    m = h * 60
    for sh in DB['shifts']:
        for iv in sh.get('intervals', []):
            s, e = to_min(iv['start']), to_min(iv['end'])
            if s <= e:
                if s <= m <= e:
                    return sh
            else:  # overnight
                if m >= s or m <= e:
                    return sh
    return None


def patrols_at(slot_idx):
    out = []
    for e in DB['equipment']:
        if not e.get('active'):
            continue
        kind = e.get('checks', {}).get(str(slot_idx))
        if kind:
            out.append({"equipment": e, "kind": kind})
    return out


def punch_status(email):
    email = (email or '').strip().lower()
    last = None
    for p in DB.get('punches', []):
        if (p.get('email') or '').strip().lower() == email:
            if last is None or (p.get('ts') or '') >= (last.get('ts') or ''):
                last = p
    if last and last.get('action') == 'in':
        return True, last
    return False, last


def on_patrol_now():
    seen = {}
    for p in DB.get('punches', []):
        em = (p.get('email') or '').strip().lower()
        if not em:
            continue
        if em not in seen or (p.get('ts') or '') >= (seen[em].get('ts') or ''):
            seen[em] = p
    out = []
    for em, p in seen.items():
        if p.get('action') == 'in':
            out.append(p)
    out.sort(key=lambda p: p.get('ts') or '')
    return out


def paired_records():
    """Pair in/out punches per email for payroll, newest first."""
    by_email = {}
    for p in sorted(DB.get('punches', []), key=lambda x: x.get('ts') or ''):
        by_email.setdefault((p.get('email') or '').strip().lower(), []).append(p)
    rows = []
    for em, ps in by_email.items():
        i = 0
        while i < len(ps):
            if ps[i].get('action') == 'in':
                cin = ps[i].get('ts')
                cout = None
                hours = None
                if i + 1 < len(ps) and ps[i + 1].get('action') == 'out':
                    cout = ps[i + 1].get('ts')
                    try:
                        dt_in = datetime.fromisoformat(cin)
                        dt_out = datetime.fromisoformat(cout)
                        hours = round((dt_out - dt_in).total_seconds() / 3600, 2)
                    except (ValueError, TypeError):
                        pass
                    i += 2
                else:
                    i += 1
                rows.append({"email": ps[i - 1].get('email'),
                             "name": ps[i - 1].get('name'),
                             "clock_in": cin, "clock_out": cout,
                             "hours": hours})
            else:
                i += 1
    rows.sort(key=lambda r: r['clock_in'] or '', reverse=True)
    return rows


# ================= DASHBOARD =================

@app.route('/')
def dashboard():
    idx = current_slot_index()
    now = local_now()
    shift = shift_for_hour(now.hour)
    due = patrols_at(idx)
    # next slot with any patrol
    nxt, nxt_list = None, []
    for k in range(1, 25):
        j = (idx + k) % 24
        pl = patrols_at(j)
        if pl:
            nxt, nxt_list = j, pl
            break
    slot_counts = [len(patrols_at(i)) for i in range(24)]
    return render_template('dashboard.html', now=now, slot_idx=idx,
                           shift=shift, due=due, nxt=nxt, nxt_list=nxt_list,
                           slots=DB['slots'], equipment=DB['equipment'],
                           shifts=DB['shifts'], counts=slot_counts,
                           on_patrol=on_patrol_now())


# ================= QR CODE =================

@app.route('/qr.png')
def qr_png():
    try:
        import qrcode
    except ImportError:
        abort(501)
    url = request.host_url.rstrip('/') + '/patrol-clock'
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ================= SETTINGS =================

@app.route('/settings')
def settings():
    return render_template('settings.html', slots=DB['slots'],
                           equipment=DB['equipment'], shifts=DB['shifts'],
                           workers=DB['workers'])


@app.route('/settings/equipment/save', methods=['POST'])
def settings_equipment_save():
    data = request.get_json(force=True)
    for item in data.get('equipment', []):
        e = find('equipment', item.get('id'))
        if not e:
            continue
        e['name'] = item.get('name', e['name'])
        e['note'] = item.get('note', '')
        e['active'] = bool(item.get('active'))
    # reorder
    order = [i.get('id') for i in data.get('equipment', [])]
    DB['equipment'].sort(key=lambda e: order.index(e['id']) if e['id'] in order else 999)
    save_db()
    return jsonify({'ok': True})


@app.route('/settings/equipment/add', methods=['POST'])
def settings_equipment_add():
    name = (request.form.get('name') or '').strip() or 'New Equipment'
    DB['equipment'].append({"id": new_id(), "name": name, "note": "",
                            "active": True, "checks": {}})
    save_db()
    return redirect(url_for('settings'))


@app.route('/settings/equipment/<eid>/delete', methods=['POST'])
def settings_equipment_delete(eid):
    DB['equipment'] = [e for e in DB['equipment'] if e['id'] != eid]
    save_db()
    return redirect(url_for('settings'))


@app.route('/settings/check', methods=['POST'])
def settings_check():
    d = request.get_json(force=True)
    e = find('equipment', d.get('equipment_id'))
    if not e:
        return jsonify({'ok': False}), 404
    slot = str(d.get('slot'))
    cur = e.get('checks', {}).get(slot, 0)
    nxt = {0: 1, 1: 2, 2: 0}[cur]
    if nxt:
        e.setdefault('checks', {})[slot] = nxt
    else:
        e.get('checks', {}).pop(slot, None)
    save_db()
    return jsonify({'ok': True, 'value': nxt})


@app.route('/settings/slots/save', methods=['POST'])
def settings_slots_save():
    data = request.get_json(force=True)
    for i, s in enumerate(data.get('slots', [])):
        if i < len(DB['slots']):
            DB['slots'][i]['label'] = s.get('label', DB['slots'][i]['label'])
            DB['slots'][i]['shift'] = s.get('shift', DB['slots'][i]['shift'])
    save_db()
    return jsonify({'ok': True})


@app.route('/settings/shifts/save', methods=['POST'])
def settings_shifts_save():
    data = request.get_json(force=True)
    DB['shifts'] = data.get('shifts', DB['shifts'])
    for sh in DB['shifts']:
        sh.setdefault('id', new_id())
    save_db()
    return jsonify({'ok': True})


@app.route('/settings/workers/add', methods=['POST'])
def settings_worker_add():
    name = (request.form.get('name') or '').strip() or 'New Worker'
    email = (request.form.get('email') or '').strip()
    DB['workers'].append({"id": new_id(), "name": name, "email": email,
                          "shift_id": request.form.get('shift_id') or ''})
    save_db()
    return redirect(url_for('settings'))


@app.route('/settings/workers/<wid>/delete', methods=['POST'])
def settings_worker_delete(wid):
    DB['workers'] = [w for w in DB['workers'] if w['id'] != wid]
    save_db()
    return redirect(url_for('settings'))


@app.route('/settings/workers/save', methods=['POST'])
def settings_workers_save():
    data = request.get_json(force=True)
    for item in data.get('workers', []):
        w = find('workers', item.get('id'))
        if not w:
            continue
        w['name'] = item.get('name', w['name'])
        w['email'] = item.get('email', '')
        w['shift_id'] = item.get('shift_id', '')
    save_db()
    return jsonify({'ok': True})


@app.route('/settings/reset', methods=['POST'])
def settings_reset():
    fresh = seed()
    # keep workers, punches, and shift assignment edits
    fresh['workers'] = DB.get('workers', [])
    fresh['punches'] = DB.get('punches', [])
    DB.clear()
    DB.update(fresh)
    save_db()
    return redirect(url_for('settings'))


@app.route('/data/backup')
def data_backup():
    save_db()
    return send_file(DB_PATH, mimetype='application/json', as_attachment=True,
                     download_name='patrol-timetable-backup.json')


# ================= TIMECARD =================

@app.route('/timecard')
def timecard():
    workers = sorted(DB['workers'], key=lambda w: w.get('name', ''))
    status = []
    for w in workers:
        em = (w.get('email') or '').strip().lower()
        if em:
            cin, last = punch_status(em)
        else:
            cin, last = False, None
        status.append({"worker": w, "clocked_in": cin,
                       "since": last.get('ts') if last else None})
    return render_template('timecard.html', status=status,
                           records=paired_records())


@app.route('/timecard/clock', methods=['POST'])
def timecard_clock():
    d = request.get_json(force=True)
    w = find('workers', d.get('worker_id'))
    if not w:
        return jsonify({'ok': False}), 404
    action = d.get('action')
    email = (w.get('email') or '').strip()
    name = w.get('name', '')
    if action == 'in':
        cin, _ = punch_status(email or name)
        if cin:
            return jsonify({'ok': False, 'error': 'Already clocked in.'}), 400
    DB['punches'].append({"id": new_id(), "email": email, "name": name,
                          "action": action, "ts": now_iso(),
                          "source": "manual"})
    save_db()
    return jsonify({'ok': True})


@app.route('/timecard/export')
def timecard_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['date', 'name', 'email', 'clock_in', 'clock_out', 'hours'])
    for r in paired_records():
        w.writerow([(r['clock_in'] or '')[:10], r['name'], r['email'],
                    r['clock_in'], r['clock_out'] or '', r['hours'] or ''])
    data = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    return send_file(data, mimetype='text/csv', as_attachment=True,
                     download_name='patrol-timecards.csv')


# ================= PATROL CLOCK (phone page) =================

@app.route('/patrol-clock')
def patrol_clock():
    return render_template('patrol_clock.html')


@app.route('/api/patrol-status')
def api_patrol_status():
    email = request.args.get('email', '')
    clocked_in, last = punch_status(email)
    return jsonify({'email': email, 'clocked_in': clocked_in,
                    'since': last.get('ts') if last else None})


@app.route('/api/patrol-mine')
def api_patrol_mine():
    email = (request.args.get('email') or '').strip().lower()
    rows = [p for p in DB.get('punches', [])
            if (p.get('email') or '').strip().lower() == email]
    rows.sort(key=lambda p: p.get('ts') or '', reverse=True)
    return jsonify({'punches': rows[:30]})


@app.route('/api/patrol-punch', methods=['POST'])
def api_patrol_punch():
    d = request.get_json(force=True)
    email = (d.get('email') or '').strip()
    name = (d.get('name') or '').strip()
    action = (d.get('action') or '').strip().lower()
    if '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'ok': False, 'error': 'Enter a valid email address.'}), 400
    if not name:
        return jsonify({'ok': False, 'error': 'Enter your name.'}), 400
    if action not in ('in', 'out'):
        return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
    clocked_in, _ = punch_status(email)
    if action == 'in' and clocked_in:
        return jsonify({'ok': False, 'error': 'Already clocked in.'}), 400
    if action == 'out' and not clocked_in:
        return jsonify({'ok': False, 'error': 'Not clocked in.'}), 400
    DB['punches'].append({"id": new_id(), "email": email, "name": name,
                          "action": action, "ts": now_iso(),
                          "source": "qr"})
    # auto-register worker by email
    if not any((w.get('email') or '').strip().lower() == email.lower()
               for w in DB['workers']):
        DB['workers'].append({"id": new_id(), "name": name, "email": email,
                              "shift_id": ""})
    else:
        for w in DB['workers']:
            if (w.get('email') or '').strip().lower() == email.lower() and not w.get('name'):
                w['name'] = name
    save_db()
    clocked_in, last = punch_status(email)
    return jsonify({'ok': True, 'clocked_in': clocked_in,
                    'since': last.get('ts') if last else None})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
