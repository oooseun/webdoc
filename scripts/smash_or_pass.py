#!/usr/bin/env python3
"""smash_or_pass.py — build a Tinder-style triage site from a deck of cards.

Born 2026-07-04 (the ask: swipe through ~20 design variants, like/dislike with arrow
keys, comment anywhere, votes land durably; the agent reads the votes, extracts
the patterns, and generates the next round *toward* the likes). Since 2026-07-13
cards can also be text propositions, so the same deck works for decision/question
triage (smash = yes/agree, pass = no).

Usage:
  python3 smash_or_pass.py DECK_DIR --out SITE_DIR [--title "..."]

DECK_DIR contains:
  deck.json  {"title": "...", "round": 1, "cards": [
                {"id": "corner-chip-a", "image": "corner-chip-a.png",
                 "name": "Corner chip · glass", "note": "top-right capsule"},
                {"id": "q-merch", "name": "Q3 · Merch",
                 "text": "Push merch hard this year?",
                 "detail": "Optional smaller paragraph of context under the question."}]}
  *.png/jpg  the card images (only needed for image cards)

Each card needs "image" or "text"; "detail" is optional context under "text".

Controls: → smash · ← pass · typing any letter autofocuses the note box (the
keystroke lands in it) · Cmd/Ctrl+→/← swipes even while typing · Cmd/Ctrl+Z undo
Esc leaves the note box. There are no bare-letter shortcuts (h/l/n/u used to
swipe/note/undo) — letters now always type into the note.

Serve SITE_DIR with webdoc's serve_site.py — votes POST to its /api/feedback and
append to feedback.jsonl as: page="<card id> <name>", feedback="VOTE: smash|pass
\nNOTE: <comment or (none)>". Reading the results: latest entry per card id wins.

Live rounds: append new cards to deck.json (bump "round") while the server runs —
the page polls every 5 s, toasts "new cards added", and deals them into the
stack. If generation is slow, append a placeholder card {"id":"__pending__",
"eta":"~2 min"} and the page shows a "next round is generating…" notice instead
of the end screen; replace it with real cards when ready.
"""
import argparse, json, shutil, sys
from pathlib import Path

PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--red:#e10600;--green:#3a9c45;--ink:#e9edf2;--muted:#9aa3ad}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#0a0c0f;color:var(--ink);
  font-family:Inter,system-ui,sans-serif;overflow:hidden}
#app{height:100%;display:flex;flex-direction:column;align-items:center;padding:14px 16px}
#top{width:min(1200px,96vw);display:flex;align-items:center;gap:14px}
#top h1{font-size:17px;margin:0;font-weight:700}
#prog{color:var(--muted);font-size:14px}#prog b{color:#fff}
#counts{margin-left:auto;font-size:14px;color:var(--muted)}
#counts .s{color:var(--green);font-weight:700}#counts .p{color:var(--red);font-weight:700}
#stage{position:relative;flex:1;width:min(1200px,96vw);margin:10px 0 6px;min-height:0}
.card{position:absolute;inset:0;display:flex;flex-direction:column;background:#14181d;
  border:1px solid #232a31;border-radius:16px;overflow:hidden;
  transition:transform .35s cubic-bezier(.2,.8,.3,1),opacity .35s;will-change:transform}
.card img{flex:1;min-height:0;object-fit:contain;background:#000;width:100%}
.card .txt{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center;
  gap:20px;padding:40px min(9vw,110px);overflow:auto}
.card .txt .q{font-size:clamp(22px,3.4vw,40px);font-weight:800;line-height:1.28}
.card .txt .d{color:var(--muted);font-size:clamp(14px,1.6vw,19px);line-height:1.55;
  white-space:pre-wrap}
.card .cap{padding:10px 16px;display:flex;gap:12px;align-items:baseline}
.card .cap b{font-size:16px}.card .cap span{color:var(--muted);font-size:13px}
.card.behind{transform:scale(.955) translateY(14px);opacity:.55;pointer-events:none}
.card.gone-l{transform:translateX(-130%) rotate(-9deg);opacity:0}
.card.gone-r{transform:translateX(130%) rotate(9deg);opacity:0}
.stamp{position:absolute;top:26px;font-size:44px;font-weight:800;letter-spacing:2px;
  padding:6px 18px;border:4px solid;border-radius:12px;opacity:0;transform:rotate(-14deg)}
.stamp.smash{right:30px;color:var(--green);border-color:var(--green)}
.stamp.pass{left:30px;color:var(--red);border-color:var(--red)}
.card.lean-r .stamp.smash,.card.lean-l .stamp.pass{opacity:1}
#bottom{width:min(1200px,96vw);display:flex;gap:10px;align-items:center;padding-bottom:8px}
#note{flex:1;background:#0d1116;color:var(--ink);border:1px solid #2b323a;border-radius:10px;
  padding:10px 12px;font-size:14px;font-family:inherit;resize:none;height:44px}
button{border:0;border-radius:10px;padding:11px 22px;font-size:15px;font-weight:700;
  cursor:pointer;color:#fff}
#passB{background:#3a2023;border:1px solid var(--red)}
#smashB{background:#15301c;border:1px solid var(--green)}
#passB:hover{background:var(--red)}#smashB:hover{background:var(--green)}
#help{width:min(1200px,96vw);text-align:center;color:#6f7882;font-size:12px;padding-bottom:10px}
#help b{color:#cfd6dd}
#done{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;
  justify-content:center;gap:14px;text-align:center}
#done h2{margin:0;font-size:26px}#done p{color:var(--muted);max-width:520px;line-height:1.5}
#toast{position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-70px);
  background:#15301c;border:1px solid var(--green);color:#fff;padding:10px 22px;
  border-radius:999px;font-size:14px;transition:transform .3s;z-index:50}
#toast.show{transform:translateX(-50%) translateY(0)}
#pending{display:none;color:var(--muted);font-size:14px;margin-top:8px}
</style></head><body>
<div id="app">
  <div id="top"><h1>__TITLE__</h1><div id="prog"><b id="pos">1</b> / <span id="tot">?</span></div>
    <div id="counts"><span class="s" id="ns">0</span> smash · <span class="p" id="np">0</span> pass</div></div>
  <div id="stage">
    <div id="done"><h2>Deck done 🎬</h2>
      <p id="doneP">Votes saved. I read them, extract what the smashes have in common, and build the next round toward it.</p>
      <div id="pending">⏳ next round is generating — this page refreshes itself when it lands…</div></div>
  </div>
  <div id="bottom">
    <textarea id="note" placeholder="Optional note on THIS card (saved with your swipe) — just start typing"></textarea>
    <button id="passB">← Pass</button><button id="smashB">Smash →</button>
  </div>
  <div id="help"><b>→</b> smash · <b>←</b> pass · type to note · <b>⌘/Ctrl+→/←</b> swipe while typing · <b>⌘/Ctrl+Z</b> undo · <b>Esc</b> leave note</div>
</div>
<div id="toast">✨ new cards added to the deck</div>
<script>
(function(){
var AID=document.title, cards=[], idx=0, votes={}, history=[], round=0;
var stage=document.getElementById('stage'), note=document.getElementById('note');
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(ch){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch];});}
function el(c){var d=document.createElement('div');d.className='card';d.dataset.id=c.id;
  var body=c.image?'<img src="'+esc(c.image)+'" alt="">'
    :'<div class="txt"><div class="q">'+esc(c.text)+'</div>'+
      (c.detail?'<div class="d">'+esc(c.detail)+'</div>':'')+'</div>';
  d.innerHTML='<div class="stamp smash">SMASH</div><div class="stamp pass">PASS</div>'+
  body+'<div class="cap"><b>'+esc(c.name)+'</b><span>'+esc(c.note||'')+'</span></div>';
  return d;}
var completedRounds={};
function announceComplete(){
  if(!cards.length||idx<cards.length||completedRounds[round])return;
  completedRounds[round]=true;
  var s=0,p=0;Object.values(votes).forEach(function(v){v==='smash'?s++:p++;});
  fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({artifact_id:AID,page:'__deck_complete__ round '+round,
      feedback:'VOTE: __deck_complete__\nNOTE: round='+round+' smash='+s+' pass='+p})}).catch(function(){});
}
function render(){
  stage.querySelectorAll('.card').forEach(function(n){n.remove();});
  var cur=cards[idx], nxt=cards[idx+1];
  if(nxt){var b=el(nxt);b.classList.add('behind');stage.appendChild(b);}
  if(cur){var t=el(cur);stage.appendChild(t);}
  document.getElementById('done').style.display=cur?'none':'flex';
  if(!cur)announceComplete();
  document.getElementById('pos').textContent=Math.min(idx+1,cards.length)||0;
  document.getElementById('tot').textContent=cards.length;
  var s=0,p=0;Object.values(votes).forEach(function(v){v==='smash'?s++:p++;});
  document.getElementById('ns').textContent=s;document.getElementById('np').textContent=p;
}
function vote(dir){
  var c=cards[idx];if(!c)return;
  var v=dir==='r'?'smash':'pass';votes[c.id]=v;history.push(idx);
  var n=(note.value||'').trim();note.value='';
  if(document.activeElement===note)note.blur();
  fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({artifact_id:AID,page:c.id+' '+c.name,
      feedback:'VOTE: '+v+'\nNOTE: '+(n||'(none)')})}).catch(function(){});
  var top=stage.querySelector('.card:not(.behind)');
  if(top){top.classList.add(dir==='r'?'gone-r':'gone-l');
    setTimeout(function(){idx++;render();},280);}else{idx++;render();}
}
function undo(){if(!history.length)return;idx=history.pop();
  var c=cards[idx];delete votes[c.id];
  fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({artifact_id:AID,page:c.id+' '+c.name,feedback:'VOTE: undo\nNOTE: (none)'})}).catch(function(){});
  render();}
document.getElementById('smashB').onclick=function(){vote('r');};
document.getElementById('passB').onclick=function(){vote('l');};
document.addEventListener('keydown',function(e){
  // Ctrl/Cmd+arrow swipes from anywhere, including mid-typing in the note box.
  // Cmd is accepted because macOS binds Ctrl+arrow to Spaces switching, which
  // eats the keystroke before the page ever sees it.
  var mod=e.ctrlKey||e.metaKey;
  if(mod&&e.key==='ArrowRight'){e.preventDefault();vote('r');return;}
  if(mod&&e.key==='ArrowLeft'){e.preventDefault();vote('l');return;}
  // Undo must be checked BEFORE the note-box early return: typing any letter
  // autofocuses the note, so undo was unreachable for the whole rest of a deck.
  if(mod&&(e.key==='z'||e.key==='Z')){e.preventDefault();undo();return;}
  if(e.target===note){if(e.key==='Escape')note.blur();return;}
  if(e.key==='ArrowRight'){e.preventDefault();vote('r');}
  else if(e.key==='ArrowLeft'){e.preventDefault();vote('l');}
  // any other printable key focuses the note box; the keystroke lands in it
  else if(e.key.length===1&&!e.ctrlKey&&!e.metaKey&&!e.altKey){note.focus();}
});
function applyDeck(d){
  var pend=d.cards.filter(function(c){return c.id==='__pending__';})[0];
  var real=d.cards.filter(function(c){return c.id!=='__pending__';});
  var known=cards.length;
  if(real.length>known){
    real.slice(known).forEach(function(c){cards.push(c);});
    if(known>0){var t=document.getElementById('toast');t.classList.add('show');
      setTimeout(function(){t.classList.remove('show');},2600);}
    render();
  }
  document.getElementById('pending').style.display=pend?'block':'none';
  if(pend&&pend.eta)document.getElementById('pending').textContent='⏳ next round is generating ('+pend.eta+') — this page refreshes itself when it lands…';
  round=d.round||round;
}
function poll(){fetch('deck.json?ts='+Date.now()).then(function(r){return r.json();})
  .then(applyDeck).catch(function(){});}
fetch('/api/feedback').then(function(r){return r.json();}).then(function(j){
  var seen={};(j.entries||[]).forEach(function(e){
    var m=/^VOTE:\s*(smash|pass|undo)/i.exec(e.feedback||'');if(!m)return;
    var id=(e.page||'').split(/\s+/)[0];
    if(m[1].toLowerCase()==='undo')delete seen[id];else seen[id]=m[1].toLowerCase();});
  votes=seen;
  poll();setInterval(poll,5000);
  // resume past already-voted leading cards after first deck load
  var t=setInterval(function(){if(cards.length){clearInterval(t);
    while(idx<cards.length&&votes[cards[idx].id])idx++;render();}},150);
}).catch(function(){poll();setInterval(poll,5000);});
})();
</script></body></html>
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('deck_dir'); ap.add_argument('--out', required=True)
    ap.add_argument('--title', default=None)
    a = ap.parse_args()
    deck_dir, out = Path(a.deck_dir), Path(a.out)
    deck = json.load(open(deck_dir / 'deck.json'))
    title = a.title or deck.get('title', 'Smash or Pass')
    out.mkdir(parents=True, exist_ok=True)
    for c in deck['cards']:
        if c['id'] == '__pending__': continue
        if c.get('image'):
            src = deck_dir / c['image']
            if not src.exists(): sys.exit(f"missing image: {src}")
            shutil.copyfile(src, out / src.name)
            c['image'] = src.name
        elif not c.get('text'):
            sys.exit(f"card {c['id']}: needs 'image' or 'text'")
    json.dump(deck, open(out / 'deck.json', 'w'), indent=1)
    (out / 'index.html').write_text(PAGE.replace('__TITLE__', title))
    json.dump({'title': title, 'feedback': 'feedback.jsonl', 'mode': 'smash-or-pass'},
              open(out / 'manifest.json', 'w'), indent=2)
    print(json.dumps({'site_dir': str(out), 'cards': len(deck['cards'])}, indent=2))

if __name__ == '__main__':
    main()
