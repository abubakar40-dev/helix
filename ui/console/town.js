// Pixel-art Agent Town — renders agents as sprites that walk, sit and type,
// driven by REAL bridge state (/state). No mock data anywhere in here.
//
// Display names are display-only per requirements §16.1: the profile ids stay
// lowercase role names and are shown in the tooltip.

const PAL = {
  floorA:"#2a3340", floorB:"#26303c", wood:"#4a3a2c", rug:"#1d2833",
  wall:"#141c25", wallTop:"#1b2530", desk:"#3a4756", deskTop:"#465566",
  screen:"#7dd3fc", screenDim:"#1e3a4d", chair:"#2f3a47",
  plant:"#2f7d4f", pot:"#5b4632", cooler:"#4a90a4",
  paper:"#c9d6e2", frame:"#3d4b5a"
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
const VW = 336, VH = 210, SCALE = 2;

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
  canvas.width = VW*SCALE; canvas.height = VH*SCALE;
  const ctx = canvas.getContext("2d");
  // Draw in virtual pixels: without this the room paints into the top-left
  // quarter of the canvas and the rest stays black (measured: 75% black).
  ctx.scale(SCALE, SCALE);
  ctx.imageSmoothingEnabled = false;

  // agents keyed by profile id; order fixed to DESKS so a desk is always the same
  const agents = {};
  let byId = [];

  // ── drawing helpers ────────────────────────────────────────────────────────
  const R = (x,y,w,h,c) => { ctx.fillStyle=c; ctx.fillRect(x|0,y|0,w|0,h|0); };
  const box = (x,y,w,h,c) => { ctx.fillStyle=c; ctx.fillRect(x|0,y|0,w|0,h|0);
    ctx.fillStyle="#0006"; ctx.fillRect(x|0,(y+h-1)|0,w|0,1|0); };

  function drawRoom(){
    R(0,0,VW,VH,PAL.wall);
    R(0,0,VW,16,PAL.wallTop);
    // carpet field
    for(let y=16;y<VH;y+=4) for(let x=0;x<VW;x+=4) R(x,y,4,4,(x+y)%8?PAL.floorA:PAL.floorB);
    // wooden corridor through the middle
    R(0,96,VW,26,PAL.wood);
    for(let x=0;x<VW;x+=8) R(x,96,1,26,"#00000022");
    R(0,120,VW,2,"#00000033");
    // wall trim + a couple of framed pictures
    R(0,16,VW,1,"#0d141b");
    [[60,20],[150,20]].forEach(([fx,fy])=>{
      box(fx,fy,14,11,PAL.frame); R(fx+2,fy+2,10,7,PAL.paper);
    });
    // rug in the middle
    R(120,150,90,40,PAL.rug); R(124,154,82,32,"#22303d");
  }

  function drawDesk(d,i){
    box(d.x-13,d.y-9,26,18,PAL.desk);
    R(d.x-13,d.y-11,26,2,PAL.deskTop);
    // monitor
    R(d.x-5,d.y-6,10,7,PAL.screenDim);
    R(d.x-4,d.y-5,8,5,PAL.screen);
    R(d.x-2,d.y-4,4,1,"#ffffff88");
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
    plant(6,168); plant(320,168); cooler(292,166);
    plant(6,70);
    // meeting table, top-right
    R(268,66,44,18,"#3a4756"); R(268,66,44,2,"#465566");
    R(272,62,4,4,"#2f3a47"); R(304,62,4,4,"#2f3a47");
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
      // typing hands move on alternate frames
      if (frame%2){ R(x,y+7,3,2,skin); R(x+5,y+7,3,2,skin); }
      else         { R(x+1,y+7,3,2,skin); R(x+4,y+7,3,2,skin); }
      R(x+1,y+1,6,6,skin);             // head
      R(x+1,y+0,6,3,hair);             // hair
      R(x+1,y+4,6,1,"#00000022");      // jaw shadow
    } else {
      // legs: 2-frame walk cycle
      const step = frame%2 ? 1 : -1;
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

  function step(){
    let busy = 0;
    for (const a of Object.values(agents)){
      const sitting = Math.abs(a.x-a.tx)<0.6 && Math.abs(a.y-a.ty)<0.6;
      if (!sitting){
        const dx = a.tx-a.x, dy = a.ty-a.y;
        const d = Math.hypot(dx,dy) || 1;
        const sp = 0.55;
        a.x += dx/d*Math.min(sp,d); a.y += dy/d*Math.min(sp,d);
        if (Math.abs(dx) > 0.2) a.dir = dx < 0 ? -1 : 1;
        a.walk = true;
      } else {
        a.walk = false;
        if (a.status==="working") busy++;
      }
      // keep inside the room
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
    if (ts-last < 90) return;         // ~11fps: pixel art does not need 60
    last = ts; frame++;
    const busy = step();
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