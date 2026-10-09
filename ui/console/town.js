// Pixel-art Agent Town — renders agents as sprites that walk, sit and type,
// driven by REAL bridge state (/state). No mock data anywhere in here.
//
// Display names are display-only per requirements §16.1: the profile ids stay
// lowercase role names and are shown in the tooltip.

// Brightened. At these values the whole office measured as near-black on screen:
// the palette sat around #26303c on a #05080d page, and at the panel's real size
// the town read as an empty black box. A screenshot showed it; the DOM never did.
const PAL = {
  floorA:"#5a6c80", floorB:"#53657a", wood:"#8a7052", rug:"#3d5268",
  wall:"#2c3c4e", wallTop:"#3b5068", desk:"#6c8098", deskTop:"#8098b0",
  screen:"#bdeeff", screenDim:"#3a6c90", chair:"#54677d",
  plant:"#4cbd7c", pot:"#9a7a56", cooler:"#6fc7dd",
  paper:"#f2f8ff", frame:"#6a7d90",
  glow:"#9fe3ff", lamp:"#ffd9a0"
};

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
    // ceiling light pools over the desk rows - cheap depth cue
    [60,158,258].forEach(cx=>{
      for(let r=13;r>0;r--) {
        ctx.fillStyle = "rgba(255,217,160," + (0.030 - r*0.0016).toFixed(4) + ")";
        ctx.fillRect(cx-r, 20-r*0.5, r*2, r);
      }
    });
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
    ctx.fillStyle = "rgba(159,227,255,0.16)";
    ctx.fillRect(d.x-9, d.y-3, 18, 7);        // glow spill on the desk surface
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

  // ── the sprite: 8 wide x 14 tall pixel person ──────────────────────────────
  function drawSprite(a, frame, sitting){
    const {shirt, hair, skin} = LOOK[a.look] || LOOK.helix;
    const x = Math.round(a.x)-4, y = Math.round(a.y)-14;
    const flip = a.dir < 0;                      // face left/right
    ctx.save();
    if (flip){ ctx.translate(x+8,0); ctx.scale(-1,1); ctx.translate(-(x+8),0); }

    // shadow
    ctx.fillStyle="#00000055"; ctx.fillRect(x+1, y+13, 6, 2);

    if (sitting){
      R(x+2,y+8,4,4,shirt);            // torso
      R(x+2,y+11,4,2,"#2b3644");       // legs tucked under desk
      R(x+1,y+8,1,3,skin); R(x+6,y+8,1,3,skin); // arms forward (typing)
      // typing hands alternate on the agent's own phase
      if (Math.floor((a.phase||0)*2)%2){ R(x,y+7,3,2,skin); R(x+5,y+7,3,2,skin); }
      else                           { R(x+1,y+7,3,2,skin); R(x+4,y+7,3,2,skin); }
      R(x+1,y+1,6,6,skin);             // head
      R(x+1,y+0,6,3,hair);             // hair
      R(x+1,y+4,6,1,"#00000022");      // jaw shadow
    } else {
      // legs: 2-frame walk cycle
      const step = Math.floor(a.phase||0)%2 ? 1 : -1;
      R(x+2,y+8,4,5,shirt);            // torso
      R(x+1,y+8,1,4,skin); R(x+6,y+8,1,4,skin);   // arms swinging
      R(x+2,y+13,2,1,"#2b3644"); R(x+4+step,y+13,2,1,"#2b3644");  // legs
      R(x+1,y+1,6,6,skin);             // head
      R(x+1,y+0,6,3,hair);
    }
    ctx.restore();

    // status pip above the head
    const st = a.status;
    const col = st==="working" ? "#f59e0b" : st==="blocked" ? "#ef4444" : "#2a3a49";
    R(x+3,y-4,2,2,col);
    if (st==="working"){ R(x+3,y-5,2,1,"#f59e0b66"); R(x+3,y-6,2,1,"#f59e0b33"); }

    // name + task
    ctx.font = "7px monospace"; ctx.textAlign="center";
    ctx.fillStyle = st==="working" ? "#e8f2fb" : "#8fa3b5";
    ctx.fillText(a.name, x+4, y-8);
    if (st==="working" && a.task){
      ctx.fillStyle="#5c6b7a"; ctx.font = "6px monospace";
      const t = a.task.length>22 ? a.task.slice(0,21)+"…" : a.task;
      ctx.fillText(t, x+4, y-14);
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
        ctx.fillText("z", a.x+5, a.y-20+bob);
        ctx.fillText("z", a.x+8, a.y-24+bob);
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
        a.tx = 150 + (a.desk%4)*22;
        a.ty = 150 + (Math.floor(a.desk/4)%2)*10;
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