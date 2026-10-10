// Pixel-art Agent Town — renders agents as sprites that walk, sit and type,
// driven by REAL bridge state (/state). No mock data anywhere in here.
//
// Display names are display-only per requirements §16.1: the profile ids stay
// lowercase role names and are shown in the tooltip.

// Brightened. At these values the whole office measured as near-black on screen:
// the palette sat around #26303c on a #05080d page, and at the panel's real size
// the town read as an empty black box. A screenshot showed it; the DOM never did.
// These were light slate greys (#5a6c80 floor, #6c8098 desk). Measured on a
// screenshot they rendered as a washed-out blockout, not a sci-fi office: the
// whole floor sat at one mid value with no dark to contrast against. The room
// moves to a deep navy base so the lit desks, cyan monitors and warm wood
// corridor actually read as light sources.
const PAL = {
  floorA:"#2a3852", floorB:"#25324a", wood:"#7d5f3f", rug:"#31445e",
  wall:"#18212f", wallTop:"#212f45", desk:"#3c4f6d", deskTop:"#4a6084",
  screen:"#9fe9ff", screenDim:"#2f5f80", chair:"#3a4c66",
  plant:"#3d9c68", pot:"#8a6a4a", cooler:"#5fb8d0",
  paper:"#e8f1fb", frame:"#4e6280",
  glow:"#7fd8ff", lamp:"#ffd9a0", dark:"#0d1420"
};

// Nine distinct standing spots in the lower half. Idle agents used to target
// 150 + (desk%4)*22, which put five of them on the same 16px of floor - the
// names overlapped and the sprites stacked into a smear.
// Varied heights, not one row: a straight line of nine sprites along the bottom
// read as a queue. These are spread across the floor and kept clear of both desk
// rows (y=40 and y=150), the wood corridor (y=96..122) and the rug.
const LOUNGE = [
  {x:34,  y:146}, {x:72,  y:174}, {x:110, y:190}, {x:152, y:196}, {x:196, y:188},
  {x:238, y:170}, {x:280, y:146}, {x:300, y:100}, {x:162, y:134}
];

// 9 agents, 9 distinct shirts. Helix (CEO) gets the brightest.
const LOOK = {
  helix:    {name:"Helix",  shirt:"#22d3ee", hair:"#1f2937", skin:"#e8b98a"},
  research: {name:"Alice",  shirt:"#f59e0b", hair:"#7c2d12", skin:"#f0c9a6"},
  pc:       {name:"Bob",    shirt:"#22c55e", hair:"#1e3a8a", skin:"#d9a066"},
  dev:      {name:"Dave",   shirt:"#a78bfa", hair:"#111827", skin:"#c68642"},
  vision:   {name:"Claire", shirt:"#ec4899", hair:"#4c1d95", skin:"#f2d0b0"},
  comm:     {name:"Ellen",  shirt:"#ef4444", hair:"#b45309", skin:"#e0ac69"},
  sysmon:   {name:"Bill",   shirt:"#06b6d4", hair:"#065f46", skin:"#8d5524"},
  career:   {name:"Carol",  shirt:"#eab308", hair:"#831843", skin:"#ffdbac"},
  document: {name:"Dan",    shirt:"#3b82f6", hair:"#374151", skin:"#c68642"}
};

// ── office geometry, in virtual pixels ────────────────────────────────────────
// Room design size. The canvas is sized to its panel 1:1 at runtime instead of
// being drawn at a fixed 2x and downscaled: at a 238px panel a 672px canvas is
// scaled 2.8x down, which averages the pixel art into a dark blur. Pixel art has
// to land on real device pixels to read as pixel art.
const VW = 336, VH = 210;

// 9 desks: 5 along the top wall, 4 along the bottom. Each seat knows which way
// the desk faces, so the sprite can sit on the correct side of it.
const DESKS = [
  {x: 26,  y: 40,  face:"down"}, {x: 92,  y: 40,  face:"down"},
  {x:158,  y: 40,  face:"down"}, {x:224,  y: 40,  face:"down"},
  {x:290,  y: 40,  face:"down"},
  {x: 40,  y:150,  face:"up"},   {x:110,  y:150,  face:"up"},
  {x:180,  y:150,  face:"up"},   {x:250,  y:150,  face:"up"}
];
const SEAT = { up:{dx:0, dy:9}, down:{dx:0, dy:-9} };

export function createTown(canvas, onStatus){
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  // Fit the room to the panel at 1:1, letterboxing rather than stretching so the
  // aspect ratio of the office never distorts.
  let fit = 1;
  const resize = () => {
    const box = canvas.parentElement.getBoundingClientRect();
    const w = Math.max(160, Math.floor(box.width));
    const h = Math.max(110, Math.floor(box.height));
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    canvas.width = w; canvas.height = h;
    // COVER, not contain. Contain left 41781 black pixels (~10% of the canvas)
    // as letterbox bars at 641x317, because the room's 336x210 aspect is wider
    // than the panel. Covering crops a little of the outer wall instead, which
    // is decorative, so the office fills its panel with no dead bars.
    fit = Math.max(w / VW, h / VH);
    ctx.imageSmoothingEnabled = false;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.translate((w - VW * fit) / 2, (h - VH * fit) / 2);
    ctx.scale(fit, fit);
  };
  resize();
  if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas.parentElement);

  // agents keyed by profile id; order fixed to DESKS so a desk is always the same
  const agents = {};
  let byId = [];

  // ── drawing helpers ────────────────────────────────────────────────────────
  const R = (x,y,w,h,c) => { ctx.fillStyle=c; ctx.fillRect(x|0,y|0,w|0,h|0); };
  const box = (x,y,w,h,c) => { ctx.fillStyle=c; ctx.fillRect(x|0,y|0,w|0,h|0);
    ctx.fillStyle="#0006"; ctx.fillRect(x|0,(y+h-1)|0,w|0,1|0); };

  function drawRoom(){
    // wall with a vertical gradient, so the room has depth instead of being a
    // flat block of one colour
    for (let y = 0; y < 22; y++) {
      R(0, y, VW, 1, y < 3 ? PAL.wallTop : PAL.wall);
    }
    // carpet field
    for(let y=22;y<VH;y+=4) for(let x=0;x<VW;x+=4)
      R(x,y,4,4,(x+y)%8?PAL.floorA:PAL.floorB);
    // wooden corridor through the middle, with plank seams and a lit edge
    R(0,96,VW,26,PAL.wood);
    for(let x=0;x<VW;x+=8) R(x,96,1,26,"#00000022");
    R(0,96,VW,1,"#c9a06a55"); R(0,120,VW,2,"#00000033");
    // wall trim + framed pictures
    R(0,20,VW,2,"#1b2733");
    [[58,4],[150,4],[250,4]].forEach(([fx,fy])=>{
      box(fx,fy,15,12,PAL.frame); R(fx+2,fy+2,11,8,PAL.paper);
      R(fx+3,fy+3,9,1,"#9fb3c8");
    });
    // Ceiling light pools over the desk rows. On the old mid-grey floor these
    // were invisible; against the new navy base they give the room its depth
    // and make the desk row the brightest thing in the frame.
    [60,158,258].forEach(cx=>{
      for(let r=15;r>0;r--) {
        ctx.fillStyle = "rgba(255,220,170," + (0.050 - r*0.0026).toFixed(4) + ")";
        ctx.fillRect(cx-r, 20-r*0.5, r*2, r);
      }
    });
    // and a cool fill from the corridor so the lower half is not pitch black
    for(let r=16;r>0;r--){
      ctx.fillStyle = "rgba(120,200,240," + (0.020 - r*0.0010).toFixed(4) + ")";
      ctx.fillRect(168-r, 104-r*0.6, r*2, r);
    }
    // rug
    R(120,150,90,40,PAL.rug); R(124,154,82,32,"#46596e");
    for(let x=126;x<204;x+=6) R(x,156,3,32,"#00000018");
  }

  function drawDesk(d,i){
    box(d.x-13,d.y-9,26,18,PAL.desk);
    R(d.x-13,d.y-11,26,2,PAL.deskTop);
    // monitor
    R(d.x-5,d.y-6,10,7,PAL.screenDim);
    R(d.x-4,d.y-5,8,5,PAL.screen);
    R(d.x-2,d.y-4,4,1,"#ffffffcc");
    ctx.fillStyle = "rgba(159,227,255,0.30)";
    ctx.fillRect(d.x-11, d.y-4, 22, 9);       // glow spill on the desk surface
    // keyboard + paper
    R(d.x-6,d.y+1,12,2,"#2b3644"); R(d.x+8,d.y-2,4,5,PAL.paper);
    // desk number plate
    R(d.x-2,d.y+7,4,3,"#2b3644");
  }

  function plant(x,y){
    R(x,y+6,6,5,PAL.pot);
    R(x+1,y+1,4,6,PAL.plant); R(x+2,y-2,2,3,"#3f9e63");
  }
  function cooler(x,y){ R(x,y+4,7,10,PAL.cooler); R(x+1,y,5,4,"#dbeafe"); }

  function drawDecor(){
    // The reference office is densely furnished; the room was sparse, which read
    // as unfinished rather than minimal. Fill it with real furniture.
    plant(6,168); plant(320,168); cooler(292,166); plant(6,70);

    // meeting table + chairs, top right
    R(268,66,44,18,"#6c8098"); R(268,66,44,2,"#8098b0");
    R(274,70,10,6,"#bdeeff"); R(292,70,8,5,"#3a6c90");
    [272,282,304,314].forEach(cx=>{ R(cx,62,4,4,"#54677d"); R(cx,84,4,4,"#54677d"); });

    // low partition walls dividing the floor into zones
    const part=(x,y,w,h)=>{ R(x,y,w,2,"#8098b0"); R(x,y+h-2,w,2,"#3d5268");
                             R(x,y,2,h,"#6c8098"); R(x+w-2,y,2,h,"#46586e"); };
    part(20,86,120,2); part(190,86,60,2); part(20,128,120,2); part(190,128,70,2);
    part(146,150,2,54); part(238,150,2,54);

    // filing cabinets + shelf along the left wall
    for (let i=0;i<3;i++){ R(4,34+i*11,12,10,"#6c8098"); R(5,36+i*11,4,2,"#2c3c4e"); }
    R(4,80,12,26,"#54677d"); R(5,82,10,1,"#8098b0");

    // notice board + wall clock, top wall
    R(196,4,26,14,"#54677d"); R(198,6,9,5,"#f2f8ff"); R(209,6,11,4,"#bdeeff");
    R(209,11,8,5,"#9fe3ff");
    ctx.fillStyle = PAL.paper;
    ctx.beginPath(); ctx.arc(300,10,5,0,Math.PI*2); ctx.fill();
    ctx.fillStyle = "#2c3c4e";
    ctx.fillRect(299,4,1,3); ctx.fillRect(300,10,3,1);

    // sofa + rug in the lounge
    R(286,186,42,14,"#4a6076"); R(288,182,38,5,"#5c7690");
    R(288,184,8,3,"#6c8098"); R(312,184,8,3,"#6c8098");
    R(258,192,26,16,"#3d5268");

    // scattered floor detail so large areas are not flat
    for (let i=0;i<7;i++){
      const x=24+((i*53)%290), y=134+((i*17)%60);
      R(x,y,2,2,"#46586e");
    }
  }

  // ── the sprite ───────────────────────────────────────────────────────────
  /* Was 8x14 virtual px, which at the panel's real fit (1.9x) rendered as a 15x27
     sliver - a coloured pillar, not a person. Now 12x20 (~23x38 on screen):
     head, hair, shirt, arms and legs are individually readable, which is what
     makes the town look inhabited rather than populated with tally marks. */
  const SW = 12, SH = 20;
  function drawSprite(a, frame, sitting){
    const {shirt, hair, skin} = LOOK[a.look] || LOOK.helix;
    const x = Math.round(a.x) - SW/2, y = Math.round(a.y) - SH;
    const flip = a.dir < 0;
    ctx.save();
    if (flip){ ctx.translate(x+SW,0); ctx.scale(-1,1); ctx.translate(-(x+SW),0); }

    // contact shadow, wider than the body so it anchors to the floor
    ctx.fillStyle="#00000066"; ctx.fillRect(x+1, y+SH-2, SW-2, 2);

    if (sitting){
      R(x+3,y+9,6,7,shirt);              // torso
      R(x+3,y+9,6,1,"#ffffff22");        // shoulder light
      R(x+3,y+16,6,2,"#2b3644");         // lap under the desk
      R(x+2,y+11,1,5,skin); R(x+9,y+11,1,5,skin);   // arms down to the keys
      const tap = Math.floor((a.phase||0)*2)%2;     // typing hands alternate
      if (tap){ R(x+1,y+14,3,2,skin); R(x+8,y+14,3,2,skin); }
      else    { R(x+1,y+15,3,2,skin); R(x+8,y+15,3,2,skin); }
    } else {
      const step = Math.floor(a.phase||0)%2 ? 1 : -1;
      R(x+3,y+9,6,7,shirt);              // torso
      R(x+3,y+9,6,1,"#ffffff22");
      R(x+2,y+10,1,5,skin); R(x+9,y+10,1,5,skin);   // arms swinging
      R(x+3,y+16,2,3,"#2b3644");         // left leg
      R(x+7+step,y+16,2,3,"#2b3644");    // right leg, offset by the walk phase
      R(x+3,y+19,2,1,"#1a222c"); R(x+7+step,y+19,2,1,"#1a222c");  // shoes
    }
    // head last so it sits over the shoulders
    R(x+3,y+2,6,7,skin);
    R(x+3,y+1,6,4,hair);                 // hair cap
    R(x+2,y+2,1,4,hair); R(x+9,y+2,1,4,hair);      // sideburns
    R(x+3,y+7,6,1,"#00000026");          // jaw shadow
    ctx.restore();

    // status pip above the head
    const st = a.status;
    const col = st==="working" ? "#f59e0b" : st==="blocked" ? "#ef4444" : "#2a3a49";
    R(x+5,y-3,2,2,col);
    if (st==="working"){ R(x+5,y-4,2,1,"#f59e0b66"); R(x+5,y-5,2,1,"#f59e0b33"); }

    // name, and the task when the agent is actually working on one
    ctx.font = "7px monospace"; ctx.textAlign="center";
    ctx.fillStyle = st==="working" ? "#e8f2fb" : "#8fa3b5";
    ctx.fillText(a.name, x+SW/2, y-6);
    if (st==="working" && a.task){
      ctx.fillStyle="#5c6b7a"; ctx.font = "6px monospace";
      const t = a.task.length>22 ? a.task.slice(0,21)+"…" : a.task;
      ctx.fillText(t, x+SW/2, y-12);
    }
    ctx.textAlign="left";
  }

  // ── movement ───────────────────────────────────────────────────────────────
  function seatFor(i, status){
    const d = DESKS[i]; const s = SEAT[d.face==="up" ? "up" : "down"];
    return { x:d.x+s.dx, y:d.y+s.dy+(d.face==="up"?4:-4) };
  }

  /* Delta-time movement on a 60fps rAF.
     It used to advance a FIXED 0.55px step and repaint at ~11fps, so an agent
     crossing the office took 40 discrete jumps per second-of-wallclock and read
     as a stutter rather than a walk. Speed is now in pixels per SECOND and the
     step is scaled by real elapsed time, so movement is frame-rate independent
     and continuous. */
  const SPEED = 46;            // virtual px per second
  const ARRIVE = 0.35;         // within this distance, the agent is seated

  function step(dt){
    let busy = 0;
    const k = Math.min(dt, 0.1) * SPEED;   // clamp so a stalled tab cannot teleport
    for (const a of Object.values(agents)){
      const dx = a.tx-a.x, dy = a.ty-a.y;
      const d = Math.hypot(dx,dy);
      if (d > ARRIVE){
        const move = Math.min(k, d);       // never overshoot the seat
        a.x += dx/d*move; a.y += dy/d*move;
        if (Math.abs(dx) > 0.2) a.dir = dx < 0 ? -1 : 1;
        a.walk = true;
        a.phase = (a.phase || 0) + move * 0.14;   // drives the leg cycle
      } else {
        a.walk = false;
        a.phase = (a.phase || 0) + dt;            // idle sway
        if (a.status==="working") busy++;
      }
      a.x = Math.max(6, Math.min(VW-6, a.x));
      a.y = Math.max(20, Math.min(VH-4, a.y));
    }
    return busy;
  }

  // ── the loop ───────────────────────────────────────────────────────────────
  let frame = 0, raf = 0, last = 0;

  function paint(){
    drawRoom();
    drawDecor();
    DESKS.forEach((d,i)=>drawDesk(d,i));
    // depth sort so the sprite in front of a desk draws over it
    const list = Object.values(agents).sort((a,b)=>a.y-b.y);
    for (const a of list) drawSprite(a, frame, !a.walk);
    // idle "Zzz" on a couple of agents, deterministic per agent
    ctx.font="6px monospace";
    for (const a of list){
      if (a.status==="idle" && !a.walk && (a.look==="pc" || a.look==="career")){
        ctx.fillStyle="#5c6b7a";
        const bob = Math.sin((frame/18)+a.x)*1.5;
        ctx.fillText("z", a.x+6, a.y-24+bob);
        ctx.fillText("z", a.x+10, a.y-29+bob);
      }
    }
  }

  function loop(ts){
    raf = requestAnimationFrame(loop);
    // 60fps. Smoothness comes from delta-time motion, not from a low repaint
    // rate, so there is no reason to throttle the loop any more.
    const dt = last ? Math.min((ts-last)/1000, 0.1) : 0.016;
    last = ts; frame++;
    const busy = step(dt);
    paint();
    if (onStatus) onStatus(busy);
  }

  // ── public API ─────────────────────────────────────────────────────────────
  function update(profiles){
    const seen = new Set();
    profiles.forEach((p, idx) => {
      seen.add(p.id);
      let a = agents[p.id];
      if (!a){
        const i = Math.min(idx, DESKS.length-1);
        const look = Object.keys(LOOK).includes(p.id) ? p.id : "helix";
        const s = seatFor(i, p.status);
        a = agents[p.id] = {
          id:p.id, look, name:(LOOK[look]||{}).name || p.name || p.id,
          x:s.x, y:VH-10, tx:s.x, ty:s.ty===undefined?s.y:s.ty,
          status:p.status, task:p.task, dir:1, walk:true, desk:i
        };
        // start off-screen-ish so the first paint shows them walking in
        a.y = VH - 8;
      }
      const st = p.status || "idle";
      // working/blocked agents go to their desk; idle ones drift to the lounge
      const target = seatFor(a.desk, st);
      if (st === "working" || st === "blocked"){ a.tx = target.x; a.ty = target.y; }
      else {
        const sp = LOUNGE[a.desk % LOUNGE.length];
        a.tx = sp.x; a.ty = sp.y;
      }
      a.status = st;
      a.task = p.task ? (p.task.title || p.task) : null;
    });
    // drop agents that no longer exist
    for (const id of Object.keys(agents)) if (!seen.has(id)) delete agents[id];
  }

  function start(){ if (!raf){ last = 0; raf = requestAnimationFrame(loop); } }
  function stop(){ cancelAnimationFrame(raf); raf = 0; }

  paint();
  return { update, start, stop,
           setAgentStatus(id, st, task){
             const a = agents[id]; if (!a) return;
             a.status = st; a.task = task || a.task;
           }};
}