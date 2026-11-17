import * as THREE from '/assets/vendor/build/three.module.js';
import { OrbitControls } from '/assets/vendor/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from '/assets/vendor/examples/jsm/loaders/GLTFLoader.js';

// Visual-only client of the existing read-only /api/neural observer.
// NEVER create synthetic RUNNING activity; DOM clock is controlled by neural.js.
const $=id=>document.getElementById(id);
const stage=$('brain3d');
let renderer,scene,camera,controls,group,meshContainer;
let wireMaterial=null,nodeMaterial=null,shellMaterial=null,sparks=[];
let nodeCount=0,connectionCount=0,ready=false,xray=false;
const sectorIds=['ingest','sports','evidence','portfolio','settlement','crypto','stocks'];
function state(){const s=window.__hopeNeuralState||{};return {
  levels:(s.connected && Array.isArray(s.levels) ? sectorIds.map((_,i)=>s.levels[i]===1?1:0) : Array(7).fill(0)),
  selected:sectorIds.indexOf(s.selection)
}}
function status(message){const el=$('render-status');if(el)el.textContent=message;}
const initialize=()=>{
 renderer=new THREE.WebGLRenderer({alpha:true,antialias:true,powerPreference:'high-performance'});
 renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));
 renderer.outputColorSpace=THREE.SRGBColorSpace;
 renderer.toneMapping=THREE.NoToneMapping;
 stage.appendChild(renderer.domElement);
 scene=new THREE.Scene();
 camera=new THREE.PerspectiveCamera(40,1,.1,100);
 camera.position.set(0,.12,7.7);
 controls=new OrbitControls(camera,renderer.domElement);
 controls.enableDamping=true;controls.dampingFactor=.055;controls.autoRotate=true;controls.autoRotateSpeed=.29;
 controls.minDistance=4.7;controls.maxDistance=13;controls.enablePan=false;
 group=new THREE.Group();scene.add(group);
 const resize=()=>{const r=stage.getBoundingClientRect();let w=Math.max(1,r.width),h=Math.max(1,r.height);renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();};
 new ResizeObserver(resize).observe(stage);resize();
 renderer.setAnimationLoop(t=>{
   const {levels,selected}=state();
   if(wireMaterial){wireMaterial.uniforms.uTime.value=t*.001;wireMaterial.uniforms.uActives.value=levels;wireMaterial.uniforms.uSelected.value=selected;}
   if(nodeMaterial){nodeMaterial.uniforms.uTime.value=t*.001;nodeMaterial.uniforms.uActives.value=levels;nodeMaterial.uniforms.uSelected.value=selected;}
   for(const spark of sparks){
     if(levels[spark.sector] && !window.matchMedia('(prefers-reduced-motion: reduce)').matches){
       let v=(t*.00045*spark.speed+spark.phase)%1;
       spark.sprite.visible=true;spark.sprite.position.lerpVectors(spark.a,spark.b,v);
       spark.sprite.material.opacity=(.4+.55*Math.sin(v*Math.PI));spark.sprite.scale.setScalar(.09+.04*Math.sin(v*Math.PI));
     }else spark.sprite.visible=false;
   }
   if(window.matchMedia('(prefers-reduced-motion: reduce)').matches) controls.autoRotate=false;
   controls.update();renderer.render(scene,camera);
 });
 $('xray').addEventListener('click',()=>{xray=!xray;$('xray').setAttribute('aria-pressed',String(xray));if(shellMaterial)shellMaterial.uniforms.uXray.value=xray?1:0;});
 $('reset').addEventListener('click',()=>{camera.position.set(0,.12,7.7);controls.target.set(0,0,0);controls.update();});
};
try{initialize();}catch(e){status('2D / WEBGL НЕДОСТУПЕН');console.warn('HOPE WebGL:',e);}
// The anatomical surface is intentionally transparent and dark.  The network,
// not a plastic shell or colored segmentation, is the visual protagonist.
const shellVertex=`
  varying vec3 vN;
  varying vec3 vView;
  varying float vY;
  void main(){
    vec4 viewPos=modelViewMatrix*vec4(position,1.0);
    vN=normalize(normalMatrix*normal);
    vY=(modelMatrix*vec4(position,1.)).y;
    vView=normalize(-viewPos.xyz);
    gl_Position=projectionMatrix*viewPos;
  }
`;
const shellFragment=`
  uniform float uXray;
  uniform float uCutY;
  varying float vY;
  varying vec3 vN;
  varying vec3 vView;
  void main(){
    if(vY<uCutY)discard;
    float fresnel=pow(1.0-abs(dot(normalize(vN),normalize(vView))),2.25);
    float alpha=mix(.075 + .29*fresnel,.012 + .13*fresnel,uXray);
    vec3 base=vec3(.035,.13,.235);
    vec3 rim=vec3(.21,.65,.94);
    gl_FragColor=vec4(mix(base,rim,clamp(fresnel*1.25,0.0,1.0)),alpha);
  }
`;
const networkVertex=`
  attribute float aSector;
  attribute float aPhase;
  attribute float aTravel;
  varying float vSector;
  varying float vPhase;
  varying float vTravel;
  uniform float uTime;
  void main(){
    vSector=aSector;vPhase=aPhase;vTravel=aTravel;
    gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);
  }
`;
const networkFragment=`
  uniform float uTime;
  uniform float uActives[7];
  uniform float uSelected;
  varying float vSector;
  varying float vPhase;
  varying float vTravel;
  void main(){
    bool on=false;for(int i=0;i<7;i++)if(abs(float(i)-vSector)<.25 && uActives[i]>.5)on=true;
    bool selected=abs(uSelected-vSector)<.25;
    float phase=fract(uTime*.34+vPhase);
    float d=abs(vTravel-phase);
    float pulse=exp(-95.0*d*d);
    float opacity=on?.68+.32*pulse:(selected?.30:.13);
    gl_FragColor=vec4(vec3(.35,.79,1.0)+vec3(.42,.18,0.0)*(on?pulse:0.0),opacity);
  }
`;
const dotVertex=`
  attribute float aSector;
  attribute float aPhase;
  varying float vAlpha;
  uniform float uActives[7];
  uniform float uSelected;
  uniform float uTime;
  void main(){
    bool on=false;for(int i=0;i<7;i++)if(abs(float(i)-aSector)<.25 && uActives[i]>.5)on=true;
    bool selected=abs(uSelected-aSector)<.25;
    float shimmer=.5+.5*sin(uTime*4.5+aPhase*20.0);
    vAlpha=on?.7+.3*shimmer:(selected?.35:.15);
    vec4 mv=modelViewMatrix*vec4(position,1.0);
    gl_PointSize=clamp((on?4.0:2.1)*(7.0/max(1.0,-mv.z)),1.4,7.0);
    gl_Position=projectionMatrix*mv;
  }
`;
const dotFragment=`
  varying float vAlpha;
  void main(){
    float r=length(gl_PointCoord-vec2(.5));
    float circle=1.0-smoothstep(.08,.5,r);
    float core=1.0-smoothstep(0.0,.16,r);
    gl_FragColor=vec4(mix(vec3(.26,.68,1.0),vec3(.88,.98,1.0),core),circle*vAlpha);
  }
`;
function seededRandom(seed){let n=seed>>>0;return()=>{n=(Math.imul(n,1664525)+1013904223)>>>0;return n/4294967296;};}
const rand=seededRandom(721828);
function sectorOf(p){
  // Voronoi regions on actual anatomical mesh; no false anatomical labels.
  const centers=[[-.64,.43,.25],[.55,.48,.12],[-.05,.55,-.42],[.6,-.18,.43],[-.32,-.53,-.4],[-.58,-.16,.12],[.25,-.18,-.48]];
  let best=1e9,s=0;
  for(let i=0;i<centers.length;i++){
    const [x,y,z]=centers[i];const d=(p.x-x)**2+(p.y-y)**2+(p.z-z)**2;
    if(d<best){best=d;s=i;}
  }
  return s;
}
function radialTexture(){
  const canvas=document.createElement('canvas');canvas.width=128;canvas.height=128;
  const cx=canvas.getContext('2d');const gr=cx.createRadialGradient(64,64,0,64,64,64);
  gr.addColorStop(0,'rgba(255,255,255,1)');gr.addColorStop(.1,'rgba(210,245,255,.95)');gr.addColorStop(.31,'rgba(105,205,255,.45)');gr.addColorStop(1,'rgba(10,90,190,0)');
  cx.fillStyle=gr;cx.fillRect(0,0,128,128);return new THREE.CanvasTexture(canvas);
}
function sampleMesh(meshes,container,bounding,cutY){
  const triangles=[];
  let totalArea=0;
  const a=new THREE.Vector3(),b=new THREE.Vector3(),c=new THREE.Vector3();
  const ab=new THREE.Vector3(),ac=new THREE.Vector3();
  for(const mesh of meshes){
    const geom=mesh.geometry,pos=geom.attributes.position,idx=geom.index;
    if(!pos)continue;
    const faces=idx?idx.count/3:pos.count/3;
    // A bounded sample of triangles, with area-weighted selection.
    const step=Math.max(1,Math.ceil(faces/9000));
    for(let k=0;k<faces;k+=step){
      const i0=idx?idx.getX(k*3):k*3,i1=idx?idx.getX(k*3+1):k*3+1,i2=idx?idx.getX(k*3+2):k*3+2;
      a.fromBufferAttribute(pos,i0).applyMatrix4(mesh.matrixWorld);container.worldToLocal(a);
      b.fromBufferAttribute(pos,i1).applyMatrix4(mesh.matrixWorld);container.worldToLocal(b);
      c.fromBufferAttribute(pos,i2).applyMatrix4(mesh.matrixWorld);container.worldToLocal(c);
      if(Math.min(a.y,b.y,c.y)<cutY)continue;
      ab.subVectors(b,a);ac.subVectors(c,a);const area=ab.cross(ac).length()*.5;
      if(area<1e-9)continue;
      totalArea+=area;triangles.push({a:a.clone(),b:b.clone(),c:c.clone(),end:totalArea});
    }
  }
  if(triangles.length<10)throw Error('Недостаточно треугольников в GLB');
  const bounds=bnd=>new THREE.Vector3((bnd.x-bounding.min.x)/(bounding.max.x-bounding.min.x)*2-1,(bnd.y-bounding.min.y)/(bounding.max.y-bounding.min.y)*2-1,(bnd.z-bounding.min.z)/(bounding.max.z-bounding.min.z)*2-1);
  const result=[];
  const PAIRS=5400;
  for(let i=0;i<PAIRS;i++){
    const x=rand()*totalArea;
    let lo=0,hi=triangles.length-1;
    while(lo<hi){const mid=(lo+hi)>>1;if(triangles[mid].end<x)lo=mid+1;else hi=mid;}
    const t=triangles[lo];let u=rand(),v=rand();if(u+v>1){u=1-u;v=1-v;}
    const position=t.a.clone().addScaledVector(t.b.clone().sub(t.a),u).addScaledVector(t.c.clone().sub(t.a),v);
    const normal=t.b.clone().sub(t.a).cross(t.c.clone().sub(t.a)).normalize();
    position.addScaledVector(normal,.014);
    const normalized=bounds(position);
    result.push({p:position,sec:sectorOf(normalized),phase:rand()});
  }
  return result;
}
function assembleNetwork(samples){
  const cells=new Map(),spacing=.28;
  const key=(x,y,z)=>`${x},${y},${z}`;
  const coord=p=>[Math.floor(p.x/spacing),Math.floor(p.y/spacing),Math.floor(p.z/spacing)];
  samples.forEach((s,i)=>{const [x,y,z]=coord(s.p),k=key(x,y,z);if(!cells.has(k))cells.set(k,[]);cells.get(k).push(i)});
  const lines=[],sector=[],phase=[],travel=[];
  const links=Array.from({length:7},()=>[]);
  let count=0;
  for(let i=0;i<samples.length;i++){
    const q=samples[i],xyz=coord(q.p),options=[];
    for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++)for(let dz=-1;dz<=1;dz++){
      for(const j of cells.get(key(xyz[0]+dx,xyz[1]+dy,xyz[2]+dz))||[]){
        if(j<=i||samples[j].sec!==q.sec)continue;
        const d=q.p.distanceToSquared(samples[j].p);
        if(d>.0025&&d<.052)options.push({j,d});
      }
    }
    options.sort((a,b)=>a.d-b.d);
    const n=Math.min(options.length,2);
    for(let l=0;l<n;l++){
      const p=samples[options[l].j];
      lines.push(q.p.x,q.p.y,q.p.z,p.p.x,p.p.y,p.p.z);
      sector.push(q.sec,q.sec);let ph=rand();phase.push(ph,ph);travel.push(0,1);
      count++;
      if(rand()<.026)links[q.sec].push({a:q.p,b:p.p,phase:ph,speed:.6+rand()*.8,sector:q.sec});
    }
  }
  const g=new THREE.BufferGeometry();
  g.setAttribute('position',new THREE.Float32BufferAttribute(lines,3));
  g.setAttribute('aSector',new THREE.Float32BufferAttribute(sector,1));
  g.setAttribute('aPhase',new THREE.Float32BufferAttribute(phase,1));
  g.setAttribute('aTravel',new THREE.Float32BufferAttribute(travel,1));
  wireMaterial=new THREE.ShaderMaterial({vertexShader:networkVertex,fragmentShader:networkFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending,uniforms:{uTime:{value:0},uActives:{value:Array(7).fill(0)},uSelected:{value:-1}},toneMapped:false});
  const network=new THREE.LineSegments(g,wireMaterial);network.renderOrder=4;group.add(network);
  const pg=new THREE.BufferGeometry(),poses=[],secs=[],phases=[];
  samples.forEach(q=>{poses.push(q.p.x,q.p.y,q.p.z);secs.push(q.sec);phases.push(q.phase)});
  pg.setAttribute('position',new THREE.Float32BufferAttribute(poses,3));
  pg.setAttribute('aSector',new THREE.Float32BufferAttribute(secs,1));
  pg.setAttribute('aPhase',new THREE.Float32BufferAttribute(phases,1));
  nodeMaterial=new THREE.ShaderMaterial({vertexShader:dotVertex,fragmentShader:dotFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending,uniforms:{uTime:{value:0},uActives:{value:Array(7).fill(0)},uSelected:{value:-1}},toneMapped:false});
  const points=new THREE.Points(pg,nodeMaterial);points.renderOrder=5;group.add(points);
  const texture=radialTexture();
  links.forEach((arr,i)=>arr.slice(0,48).forEach(link=>{
    const mat=new THREE.SpriteMaterial({map:texture,color:0xd8f5ff,transparent:true,opacity:0,blending:THREE.AdditiveBlending,depthTest:false,depthWrite:false});
    const sprite=new THREE.Sprite(mat);sprite.visible=false;group.add(sprite);
    sparks.push({...link,sprite});
  }));
  nodeCount=samples.length;connectionCount=count;
  status('3D / '+nodeCount.toLocaleString('ru-RU')+' НЕЙРОНОВ · '+count.toLocaleString('ru-RU')+' СВЯЗЕЙ');
}
// Identify cortex by mesh size and location, and cut away any remaining inferior
// geometry. This prevents the cerebellum, pons and stem from reappearing when
// the 3D model rotates, and excludes those parts from sampled neuron pathways.
function installed(glb){
  if(!renderer)return;
  meshContainer=glb.scene;
  meshContainer.updateMatrixWorld(true);
  let originalBox=new THREE.Box3().setFromObject(meshContainer);
  const dims=originalBox.getSize(new THREE.Vector3());
  const max=Math.max(dims.x,dims.y,dims.z);
  if(max<=0)throw Error('GLB is empty');
  meshContainer.scale.multiplyScalar(3.6/max);meshContainer.updateMatrixWorld(true);
  originalBox.setFromObject(meshContainer);
  const center=originalBox.getCenter(new THREE.Vector3());meshContainer.position.sub(center);
  group.add(meshContainer);meshContainer.updateMatrixWorld(true);
  const box=new THREE.Box3().setFromObject(meshContainer),height=box.max.y-box.min.y;
  if(height<=0)throw Error('Invalid GLB height');
  let all=[];meshContainer.traverse(o=>{if(o.isMesh){
    if(!o.geometry?.attributes?.position) return;
    if(!o.geometry.attributes.normal)o.geometry.computeVertexNormals();
    const b=new THREE.Box3().setFromObject(o), size=b.getSize(new THREE.Vector3());
    const n=o.geometry.index?.count||o.geometry.attributes.position.count;
    all.push({o,b,size,n,name:(o.name+' '+(o.parent?.name||'')).toLowerCase()});
  }});
  if(!all.length)throw Error('No brain mesh');
  const maxN=Math.max(...all.map(x=>x.n));
  const omit=/cerebell|brain.?stem|medulla|pons|spinal|stem|vermis/;
  // Keep major upper structures; isolated inferior meshes are discarded entirely.
  const upper=box.max.y-height*.48;
  let picked=all.filter(x=> !omit.test(x.name) &&
    x.b.max.y>=upper && (x.n>=maxN*.24 || x.b.max.y>box.max.y-height*.18));
  if(!picked.length)picked=[...all].sort((a,b)=>b.n-a.n).slice(0,1);
  // If cortex is fused to inferior structures in one mesh, crop in 3D on a
  // horizontal anatomical plane, affecting both shell and the node sampler.
  const cutY=(picked.length===all.length && all.length<=2)
    ?box.min.y+height*.40 :box.min.y+height*.20;
  for(const x of all)if(!picked.includes(x))x.o.visible=false;
  shellMaterial=new THREE.ShaderMaterial({
    vertexShader:shellVertex,fragmentShader:shellFragment,transparent:true,
    depthWrite:false,depthTest:true,side:THREE.FrontSide,
    uniforms:{uXray:{value:0},uCutY:{value:cutY}},toneMapped:false
  });
  for(const x of picked){x.o.material=shellMaterial;x.o.renderOrder=1;}
  meshContainer.updateMatrixWorld(true);
  const samples=sampleMesh(picked.map(x=>x.o),group,box,cutY);
  assembleNetwork(samples);
  stage.classList.add('ready3d');
  ready=true;status('3D / CORTEX ONLY · '+picked.length+'/'+all.length+' MESH');
  const ray=new THREE.Raycaster();const mouse=new THREE.Vector2();
  renderer.domElement.addEventListener('dblclick',e=>{
    const rect=renderer.domElement.getBoundingClientRect();
    mouse.set((e.clientX-rect.left)/rect.width*2-1,-((e.clientY-rect.top)/rect.height*2-1));
    ray.setFromCamera(mouse,camera);const hits=ray.intersectObjects(picked.map(x=>x.o),false);
    if(!hits.length || hits[0].point.y<cutY)return;
    const p=group.worldToLocal(hits[0].point.clone());
    const norm=new THREE.Vector3((p.x-box.min.x)/(box.max.x-box.min.x)*2-1,
      (p.y-box.min.y)/(box.max.y-box.min.y)*2-1,(p.z-box.min.z)/(box.max.z-box.min.z)*2-1);
    const s=sectorOf(norm);window.__hopeSectorSelected?.(sectorIds[s]);
  });
}
if(renderer){new GLTFLoader().load('/assets/brain.glb',installed,undefined,e=>{
  console.error('HOPE anatomy:',e);status('2D / ОШИБКА МОДЕЛИ');
});}
