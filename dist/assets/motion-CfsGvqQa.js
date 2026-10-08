const h=Math.PI*2,w=t=>Math.max(0,Math.min(1,t)),d=t=>(t=w(t),t*t*(3-2*t)),c=(t,e,i)=>Math.sin(w((t-e)/(i-e))*Math.PI),o=Object.freeze({y:0,rx:0,ry:0,rz:0,eye:1,gazeX:0,gazeY:0,width:1,height:1,depth:1,taper:0,bend:0,forward:0,curve:0,ripple:0,phase:0});function u(t,e,i={x:0,y:0}){const p=Math.sin(e*h/5),n={...o,height:1+p*.012,width:1-p*.006,ry:Math.sin(e*h/10)*.025,gazeX:i.x*.027,gazeY:i.y*.02};switch(t){case"listening":{const a=Math.pow(Math.max(0,Math.sin(e*h/5)),6);Object.assign(n,{height:1.12-a*.045,width:.94,taper:.18,bend:-.14,forward:.22+a*.07,eye:1.16,ry:0,gazeX:i.x*.02});break}case"thinking":{const a=Math.sin(e*h/5);Object.assign(n,{height:1.15,width:.91,taper:-.1,bend:.22+a*.2,curve:-.22-a*.11,forward:-.08,ripple:.045,phase:-e*h/5,gazeX:-.022,gazeY:.04,eye:.68,ry:-.1});break}case"speaking":{const a=.5-.5*Math.cos(e*h/5),r=.5+.5*Math.sin(e*h*1.6),s=a*r;Object.assign(n,{height:.94+s*.3,width:1.04-s*.14,depth:1+s*.05,taper:s*.22,bend:Math.sin(e*h/5)*a*.23,curve:Math.sin(e*h*.8)*a*.13,ripple:a*(.035+r*.065),phase:-e*h*.8,forward:s*.11,eye:.92+s*.2,ry:Math.sin(e*h/5)*.045});break}case"sleeping":{const a=(p+1)/2;Object.assign(n,{height:.48+a*.025,width:1.3-a*.025,depth:1.13,taper:-.18,bend:.3,curve:-.12,forward:.12,eye:0,ry:.03,gazeX:0,gazeY:0});break}case"happy":{const a=c(e,0,3.5),r=Math.max(0,Math.sin(e*5.4))**2*a;Object.assign(n,{y:r*.25,height:1+r*.14,width:1-r*.1,taper:-.08*a,bend:Math.sin(e*8)*.16*a,curve:-Math.sin(e*8)*.1*a,eye:1-a,ry:0});break}case"curious":{const a=c(e,0,4);Object.assign(n,{height:1+a*.1,width:1-a*.055,bend:Math.sin(e*1.8)*.34*a,curve:-Math.sin(e*1.8)*.15*a,ry:Math.sin(e*2)*.25*a,gazeX:Math.sin(e*2)*.04,eye:1.13});break}case"bounce":{const a=c(e,0,.6),r=c(e,.55,1.65),s=c(e,1.65,2.35);Object.assign(n,{y:r*.63,height:1-a*.3+r*.2-s*.28,width:1+a*.23-r*.12+s*.21,taper:-s*.13,depth:1+a*.08+s*.07,bend:r*.08});break}case"spin":{const a=d((e-.25)/2.15),r=Math.sin(a*Math.PI);Object.assign(n,{ry:a*h,y:r*.16,height:1+r*.1,width:1-r*.06,curve:Math.sin(a*h)*.1});break}case"stretch":{const a=c(e,0,4);Object.assign(n,{height:1+a*.42,width:1-a*.22,taper:-a*.24,bend:a*.17,curve:-a*.12,eye:1-a*.9,ry:0});break}case"wobble":{const a=c(e,0,3.5),r=Math.sin(e*6)*a;Object.assign(n,{bend:r*.46,curve:-r*.22,height:1-Math.abs(r)*.08,width:1+Math.abs(r)*.06,ry:0});break}case"wake":{const a=d((e-.4)/1.5),r=c(e,1,3.8);Object.assign(n,{height:.49+a*.51+r*.25,width:1.29-a*.29-r*.13,depth:1.13-a*.13,taper:-.18*(1-a)-r*.13,bend:.3*(1-a),curve:-.12*(1-a),forward:.12*(1-a),eye:a,gazeX:0,gazeY:0,ry:0});break}}return n}function g(t,e,i,p,n={}){const a=w((t.y-i)/p),r=1+e.taper*(a-.5)+e.ripple*Math.sin(Math.PI*a)*Math.sin(2*h*a+e.phase);return n.x=t.x*e.width*r+e.bend*a*a+e.curve*Math.sin(Math.PI*a),n.y=i+(t.y-i)*e.height,n.z=t.z*e.depth+e.forward*a*a,n}const l=`
uniform vec4 warpShape, warpBend;
uniform vec3 warpRange;
vec3 warpPosition(vec3 p) {
  if (all(equal(warpShape, vec4(1.0, 1.0, 1.0, 0.0))) && all(equal(warpBend, vec4(0.0)))) return p;
  float u = clamp((p.y - warpRange.x) / warpRange.y, 0.0, 1.0);
  float contour = 1.0 + warpShape.w * (u - 0.5)
    + warpBend.w * sin(3.14159265 * u) * sin(12.56637061 * u + warpRange.z);
  return vec3(p.x * warpShape.x * contour + warpBend.x * u * u + warpBend.z * sin(3.14159265 * u),
    warpRange.x + (p.y - warpRange.x) * warpShape.y,
    p.z * warpShape.z + warpBend.y * u * u);
}
vec3 warpNormal(vec3 n, vec3 p) {
  if (all(equal(warpShape, vec4(1.0, 1.0, 1.0, 0.0))) && all(equal(warpBend, vec4(0.0)))) return normalize(n);
  float rawU = (p.y - warpRange.x) / warpRange.y;
  float u = clamp(rawU, 0.0, 1.0);
  float q = sin(3.14159265 * u), c = cos(3.14159265 * u);
  float s = sin(12.56637061 * u + warpRange.z), w = cos(12.56637061 * u + warpRange.z);
  float contour = 1.0 + warpShape.w * (u - 0.5) + warpBend.w * q * s;
  float derivative = warpShape.w + warpBend.w * (3.14159265 * c * s + 12.56637061 * q * w);
  float dx = (p.x * warpShape.x * derivative + 2.0 * warpBend.x * u + warpBend.z * 3.14159265 * c) / warpRange.y;
  float dz = 2.0 * warpBend.y * u / warpRange.y;
  if (rawU < 0.0 || rawU > 1.0) { dx = 0.0; dz = 0.0; }
  vec3 result = vec3(n.x / (warpShape.x * contour), 0.0, n.z / warpShape.z);
  result.y = (n.y - dx * result.x - dz * result.z) / warpShape.y;
  return normalize(result);
}`;export{l as D,o as R,g as d,u as m};
