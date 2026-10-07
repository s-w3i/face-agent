(function(){const a=document.createElement("link").relList;if(a&&a.supports&&a.supports("modulepreload"))return;for(const r of document.querySelectorAll('link[rel="modulepreload"]'))h(r);new MutationObserver(r=>{for(const e of r)if(e.type==="childList")for(const t of e.addedNodes)t.tagName==="LINK"&&t.rel==="modulepreload"&&h(t)}).observe(document,{childList:!0,subtree:!0});function i(r){const e={};return r.integrity&&(e.integrity=r.integrity),r.referrerPolicy&&(e.referrerPolicy=r.referrerPolicy),r.crossOrigin==="use-credentials"?e.credentials="include":r.crossOrigin==="anonymous"?e.credentials="omit":e.credentials="same-origin",e}function h(r){if(r.ep)return;r.ep=!0;const e=i(r);fetch(r.href,e)}})();const s=Math.PI*2,o=n=>Math.max(0,Math.min(1,n)),d=n=>(n=o(n),n*n*(3-2*n)),p=(n,a,i)=>Math.sin(o((n-a)/(i-a))*Math.PI),u=Object.freeze({y:0,rx:0,ry:0,rz:0,eye:1,gazeX:0,gazeY:0,width:1,height:1,depth:1,taper:0,bend:0,forward:0,curve:0,ripple:0,phase:0});function w(n,a,i={x:0,y:0}){const h=Math.sin(a*s/5),r={...u,height:1+h*.012,width:1-h*.006,ry:Math.sin(a*s/10)*.025,gazeX:i.x*.027,gazeY:i.y*.02};switch(n){case"listening":{const e=Math.pow(Math.max(0,Math.sin(a*s/5)),6);Object.assign(r,{height:1.12-e*.045,width:.94,taper:.18,bend:-.14,forward:.22+e*.07,eye:1.16,ry:0,gazeX:i.x*.02});break}case"thinking":{const e=Math.sin(a*s/5);Object.assign(r,{height:1.15,width:.91,taper:-.1,bend:.22+e*.2,curve:-.22-e*.11,forward:-.08,ripple:.045,phase:-a*s/5,gazeX:-.022,gazeY:.04,eye:.68,ry:-.1});break}case"speaking":{const e=.5-.5*Math.cos(a*s/5),t=.5+.5*Math.sin(a*s*1.6),c=e*t;Object.assign(r,{height:.94+c*.3,width:1.04-c*.14,depth:1+c*.05,taper:c*.22,bend:Math.sin(a*s/5)*e*.23,curve:Math.sin(a*s*.8)*e*.13,ripple:e*(.035+t*.065),phase:-a*s*.8,forward:c*.11,eye:.92+c*.2,ry:Math.sin(a*s/5)*.045});break}case"sleeping":{const e=(h+1)/2;Object.assign(r,{height:.48+e*.025,width:1.3-e*.025,depth:1.13,taper:-.18,bend:.3,curve:-.12,forward:.12,eye:0,ry:.03,gazeX:0,gazeY:0});break}case"happy":{const e=p(a,0,3.5),t=Math.max(0,Math.sin(a*5.4))**2*e;Object.assign(r,{y:t*.25,height:1+t*.14,width:1-t*.1,taper:-.08*e,bend:Math.sin(a*8)*.16*e,curve:-Math.sin(a*8)*.1*e,eye:1-e,ry:0});break}case"curious":{const e=p(a,0,4);Object.assign(r,{height:1+e*.1,width:1-e*.055,bend:Math.sin(a*1.8)*.34*e,curve:-Math.sin(a*1.8)*.15*e,ry:Math.sin(a*2)*.25*e,gazeX:Math.sin(a*2)*.04,eye:1.13});break}case"bounce":{const e=p(a,0,.6),t=p(a,.55,1.65),c=p(a,1.65,2.35);Object.assign(r,{y:t*.63,height:1-e*.3+t*.2-c*.28,width:1+e*.23-t*.12+c*.21,taper:-c*.13,depth:1+e*.08+c*.07,bend:t*.08});break}case"spin":{const e=d((a-.25)/2.15),t=Math.sin(e*Math.PI);Object.assign(r,{ry:e*s,y:t*.16,height:1+t*.1,width:1-t*.06,curve:Math.sin(e*s)*.1});break}case"stretch":{const e=p(a,0,4);Object.assign(r,{height:1+e*.42,width:1-e*.22,taper:-e*.24,bend:e*.17,curve:-e*.12,eye:1-e*.9,ry:0});break}case"wobble":{const e=p(a,0,3.5),t=Math.sin(a*6)*e;Object.assign(r,{bend:t*.46,curve:-t*.22,height:1-Math.abs(t)*.08,width:1+Math.abs(t)*.06,ry:0});break}case"wake":{const e=d((a-.4)/1.5),t=p(a,1,3.8);Object.assign(r,{height:.49+e*.51+t*.25,width:1.29-e*.29-t*.13,depth:1.13-e*.13,taper:-.18*(1-e)-t*.13,bend:.3*(1-e),curve:-.12*(1-e),forward:.12*(1-e),eye:e,gazeX:0,gazeY:0,ry:0});break}}return r}function l(n,a,i,h,r={}){const e=o((n.y-i)/h),t=1+a.taper*(e-.5)+a.ripple*Math.sin(Math.PI*e)*Math.sin(2*s*e+a.phase);return r.x=n.x*a.width*t+a.bend*e*e+a.curve*Math.sin(Math.PI*e),r.y=i+(n.y-i)*a.height,r.z=n.z*a.depth+a.forward*e*e,r}const g=`
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
}`;export{g as D,u as R,l as d,w as m};
