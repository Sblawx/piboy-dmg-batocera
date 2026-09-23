// piboy-osd — overlay HUD Wayland (wlr-layer-shell) pour le PiBoy DMG / Batocera.
//
// Affiche PAR-DESSUS les jeux (couche "overlay" de Sway) : batterie (jauge
// reelle BAT0), temperature CPU + charge, signal WiFi, Bluetooth, horloge, et le volume
// (transitoire). Chaque element a un MODE d'affichage : off / ES seulement /
// en jeu seulement / les deux — pilote par /userdata/system/piboy-osd.conf.
//
// Rendu logiciel (wl_shm ARGB8888 premultiplie), icones PNG (stb_image) et
// texte OTF (stb_truetype) = assets recuperes de l'OSD RetroPie. Region d'input
// VIDE : les boutons passent au jeu.
#define _GNU_SOURCE
#include <wayland-client.h>
#include "wlr-layer-shell-unstable-v1-client-protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <poll.h>
#include <math.h>
#include <dirent.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/ip.h>
#include <netinet/ip_icmp.h>
#include <arpa/inet.h>

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#include "stb_image.h"
#define STB_TRUETYPE_IMPLEMENTATION
#include "stb_truetype.h"

#define RES_DIR_DEFAULT "/userdata/system/piboy-osd/resources"
#define CONF_PATH       "/userdata/system/piboy-osd.conf"
#define MSG_PATH        "/tmp/piboy-osd.msg"   // messages du service piboy (batterie faible...)

// modes d'affichage
enum { MODE_OFF = 0, MODE_ES = 1, MODE_GAME = 2, MODE_BOTH = 3 };

static int   OSD_H        = 46;
static int   TEXT_PX      = 20;
static int   ICON_PX      = 15;
static double VOLUME_SHOW_S = 2.0;
static int   cfg_enabled  = 1;
static int   m_battery = MODE_BOTH, m_temp = MODE_BOTH, m_cpu = MODE_BOTH;
static int   m_volume  = MODE_BOTH, m_wifi = MODE_BOTH, m_clock = MODE_GAME;
static int   m_bt = MODE_ES;
static int   cfg_bottom = 0;   // 0 = barre en haut, 1 = en bas
static char  res_dir[512] = RES_DIR_DEFAULT;

static inline int visible(int mode, int in_game) {
    return mode == MODE_BOTH || (mode == MODE_ES && !in_game) || (mode == MODE_GAME && in_game);
}

// ---------------------------------------------------------------- wayland ---
static struct wl_compositor    *compositor;
static struct wl_shm           *shm;
static struct zwlr_layer_shell_v1 *layer_shell;
static struct wl_surface         *surface;
static struct zwlr_layer_surface_v1 *layer_surface;
static struct wl_display *display;
static int running = 1, surf_w = 0, configured = 0;

struct buf { struct wl_buffer *wlb; uint32_t *px; int w, h, size, busy; };
static struct buf bufs[2];

// ------------------------------------------------------------------ assets ---
typedef struct { int w, h; unsigned char *rgba; } Image;
static stbtt_fontinfo g_font;
static unsigned char *g_font_data = NULL;
static float g_font_scale = 0;
static int   g_ascent = 0, g_charw = 10;

static Image img_temp;
static Image img_wifi[5];
static Image img_bt, img_bt_conn;
static Image img_batt; static char img_batt_key[64] = "";

static Image load_png(const char *path) {
    Image im = {0,0,NULL}; int n;
    im.rgba = stbi_load(path, &im.w, &im.h, &n, 4);
    if (!im.rgba) fprintf(stderr, "piboy-osd: PNG introuvable: %s\n", path);
    return im;
}
static void load_font(void) {
    char path[600];
    snprintf(path, sizeof path, "%s/SourceCodePro-Black.otf", res_dir);
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "piboy-osd: police introuvable: %s\n", path); return; }
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    g_font_data = malloc(sz);
    if (fread(g_font_data, 1, sz, f) != (size_t)sz) { fclose(f); return; }
    fclose(f);
    stbtt_InitFont(&g_font, g_font_data, stbtt_GetFontOffsetForIndex(g_font_data, 0));
    g_font_scale = stbtt_ScaleForPixelHeight(&g_font, (float)TEXT_PX);
    int a, d, l; stbtt_GetFontVMetrics(&g_font, &a, &d, &l);
    g_ascent = (int)(a * g_font_scale);
    int adv, lsb; stbtt_GetCodepointHMetrics(&g_font, '0', &adv, &lsb);
    g_charw = (int)(adv * g_font_scale);
}

// ------------------------------------------------------------- compositing ---
static inline void blend_px(uint32_t *dst, uint8_t sr, uint8_t sg, uint8_t sb, uint8_t sa) {
    if (!sa) return;
    uint32_t d = *dst;
    uint8_t dr = d >> 16, dg = d >> 8, db = d, da = d >> 24;
    uint32_t ia = 255 - sa;
    *dst = ((uint32_t)(sa + (uint32_t)da*ia/255) << 24)
         | ((uint32_t)(sr + (uint32_t)dr*ia/255) << 16)
         | ((uint32_t)(sg + (uint32_t)dg*ia/255) << 8)
         |  (uint8_t )(sb + (uint32_t)db*ia/255);
}
static void fill_rect(uint32_t *buf, int W, int H, int x, int y, int w, int h,
                      uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    uint8_t pr = (uint32_t)r*a/255, pg = (uint32_t)g*a/255, pb = (uint32_t)b*a/255;
    for (int yy = y; yy < y+h; yy++) { if (yy<0||yy>=H) continue;
        for (int xx = x; xx < x+w; xx++) { if (xx<0||xx>=W) continue;
            blend_px(&buf[yy*W+xx], pr, pg, pb, a); } }
}
static void pill(uint32_t *buf, int W, int H, int x, int y, int w, int h, uint8_t a) {
    int rad = h/2;
    for (int yy = 0; yy < h; yy++) {
        int py = y+yy; if (py<0||py>=H) continue;
        int inset = 0, dy = (yy<rad) ? (rad-1-yy) : (yy-(h-rad));
        if (dy > 0) { int v = rad*rad-dy*dy; inset = rad-(int)(v>0?sqrtf((float)v):0); }
        for (int xx = inset; xx < w-inset; xx++) {
            int px = x+xx; if (px<0||px>=W) continue;
            blend_px(&buf[py*W+px], 0,0,0, a); }
    }
}
static inline void img_sample(const Image *im, int x, int y, float *o) {
    if (x<0) x=0; if (y<0) y=0; if (x>=im->w) x=im->w-1; if (y>=im->h) y=im->h-1;
    const unsigned char *p = &im->rgba[(y*im->w+x)*4];
    float a = p[3];
    o[0]=p[0]*a/255.f; o[1]=p[1]*a/255.f; o[2]=p[2]*a/255.f; o[3]=a;
}
static void blit_img_scaled(uint32_t *buf, int W, int H, const Image *im, int x, int y, int dw, int dh) {
    if (!im->rgba || dw<=0 || dh<=0) return;
    for (int j = 0; j < dh; j++) {
        int py = y+j; if (py<0||py>=H) continue;
        float sy = (j+0.5f)*im->h/dh-0.5f; int y0 = (int)floorf(sy); float fy = sy-y0;
        for (int i = 0; i < dw; i++) {
            int px = x+i; if (px<0||px>=W) continue;
            float sx = (i+0.5f)*im->w/dw-0.5f; int x0 = (int)floorf(sx); float fx = sx-x0;
            float c00[4],c10[4],c01[4],c11[4],c[4];
            img_sample(im,x0,y0,c00); img_sample(im,x0+1,y0,c10);
            img_sample(im,x0,y0+1,c01); img_sample(im,x0+1,y0+1,c11);
            for (int k=0;k<4;k++){ float t=c00[k]*(1-fx)+c10[k]*fx, bo=c01[k]*(1-fx)+c11[k]*fx; c[k]=t*(1-fy)+bo*fy; }
            uint8_t a=(uint8_t)(c[3]+0.5f); if(!a) continue;
            blend_px(&buf[py*W+px],(uint8_t)(c[0]+0.5f),(uint8_t)(c[1]+0.5f),(uint8_t)(c[2]+0.5f),a);
        }
    }
}
static int icon_w(const Image *im){ return im->rgba ? im->w*ICON_PX/im->h : ICON_PX; }
static void blit_icon(uint32_t *buf,int W,int H,const Image *im,int x,int y){
    if(!im->rgba) return; blit_img_scaled(buf,W,H,im,x,y,icon_w(im),ICON_PX);
}
static int text_w(const int *cp, int n){ int w=0; for(int i=0;i<n;i++){int a,l;stbtt_GetCodepointHMetrics(&g_font,cp[i],&a,&l);w+=(int)(a*g_font_scale);} return w; }
static void draw_text(uint32_t *buf,int W,int H,const int *cp,int n,int x,int y_top,uint8_t r,uint8_t g,uint8_t b){
    if(!g_font_data) return; int pen=x;
    for(int i=0;i<n;i++){
        int cw,ch,xo,yo; unsigned char *bmp=stbtt_GetCodepointBitmap(&g_font,0,g_font_scale,cp[i],&cw,&ch,&xo,&yo);
        int adv,lsb; stbtt_GetCodepointHMetrics(&g_font,cp[i],&adv,&lsb);
        if(bmp){ for(int yy=0;yy<ch;yy++){int py=y_top+g_ascent+yo+yy; if(py<0||py>=H)continue;
            for(int xx=0;xx<cw;xx++){int px=pen+xo+xx; if(px<0||px>=W)continue; uint8_t a=bmp[yy*cw+xx]; if(!a)continue;
            blend_px(&buf[py*W+px],(uint32_t)r*a/255,(uint32_t)g*a/255,(uint32_t)b*a/255,a);}}
            stbtt_FreeBitmap(bmp,NULL); }
        pen += (int)(adv*g_font_scale);
    }
}
static int str_cp(const char *s, int *cp, int max){ int n=0; for(const char*p=s;*p&&n<max;p++)cp[n++]=(unsigned char)*p; return n; }
// decode aussi le degre UTF-8 (0xC2 0xB0) -> 0xB0
static int str_cp_deg(const char *s, int *cp, int max){
    int n=0; for(const char*p=s;*p&&n<max;){ unsigned char c=*p;
        if(c==0xC2&&(unsigned char)p[1]==0xB0){cp[n++]=0xB0;p+=2;} else {cp[n++]=c;p++;} }
    return n;
}

// -------------------------------------------------------------- sensors ---
static int read_int_file(const char *path){ FILE*f=fopen(path,"r"); if(!f)return -1; int v=-1; if(fscanf(f,"%d",&v)!=1)v=-1; fclose(f); return v; }
static void read_str_file(const char *path,char*out,int max){ out[0]=0; FILE*f=fopen(path,"r"); if(!f)return; if(fgets(out,max,f)){char*nl=strchr(out,'\n'); if(nl)*nl=0;} fclose(f); }

typedef struct { int volume, battery, temp_c, cpu_pct, charging, wifi_bars, wifi_state, bt; char clock[8]; } State;
static long long cpu_prev_idle=0, cpu_prev_tot=0;
static int read_cpu_pct(void){
    FILE*f=fopen("/proc/stat","r"); if(!f)return -1;
    char lbl[8]; long long u,ni,s,idle,io,irq,sirq,st;
    if(fscanf(f,"%7s %lld %lld %lld %lld %lld %lld %lld %lld",lbl,&u,&ni,&s,&idle,&io,&irq,&sirq,&st)<5){fclose(f);return -1;}
    fclose(f);
    long long tot=u+ni+s+idle+io+irq+sirq+st, dtot=tot-cpu_prev_tot, didle=idle-cpu_prev_idle;
    cpu_prev_tot=tot; cpu_prev_idle=idle;
    if(dtot<=0) return -1; return (int)(100*(dtot-didle)/dtot);
}
static int read_wifi_bars(void){
    FILE*f=fopen("/proc/net/wireless","r"); if(!f)return -1;
    char line[256]; int bars=-1;
    while(fgets(line,sizeof line,f)){
        char*c=strchr(line,':');
        if(c && (strstr(line,"wlan")||strstr(line,"wlp")||strstr(line,"wlo"))){
            int status; float link;
            if(sscanf(c+1,"%d %f",&status,&link)>=2){ int q=(int)link;
                bars = q<10?0 : q<25?1 : q<40?2 : q<55?3 : 4; }
        }
    }
    fclose(f); return bars;
}

// ---- connectivite Wi-Fi REELLE (sonde ICMP vers la passerelle) -------------
// /proc/net/wireless ne dit rien de vrai : la qualite de lien y reste figee
// quand la puce est en "zombie" (associee, plus aucun paquet - vu le 2026-08-17
// sur le Pi 4 : deux heures d'icone Wi-Fi affichee a tort). On sonde donc la
// passerelle par l'interface sans fil, dans un thread, toutes les 10 s.
enum { WIFI_UNKNOWN=-1, WIFI_OK=0, WIFI_KO=1, WIFI_DOWN=2, WIFI_ETH=3 };
static volatile int g_wifi_state = WIFI_UNKNOWN;
static const int WIFI_PROBE_S = 10, WIFI_FAILS_KO = 3;

// Route par defaut : renvoie 1 et remplit iface/gw si trouvee. Une route par
// une interface sans fil est preferee a une route filaire.
static int default_route(char *iface, size_t ilen, struct in_addr *gw){
    FILE*f=fopen("/proc/net/route","r"); if(!f) return 0;
    char line[256]; int found=0, found_wl=0;
    while(fgets(line,sizeof line,f)){
        char ifn[32]; unsigned long dst, g; int flags;
        if(sscanf(line,"%31s %lx %lx %x",ifn,&dst,&g,&flags)!=4) continue;
        if(dst!=0 || !(flags&2)) continue;                 // RTF_GATEWAY
        int wl = !strncmp(ifn,"wl",2);
        if(found && !(wl && !found_wl)) continue;
        snprintf(iface,ilen,"%s",ifn); gw->s_addr=(in_addr_t)g;
        found=1; found_wl=wl;
    }
    fclose(f); return found;
}
static unsigned short icmp_cksum(const void *d, int len){
    const unsigned short *p=d; unsigned long s=0;
    while(len>1){ s+=*p++; len-=2; } if(len) s+=*(const unsigned char*)p;
    s=(s>>16)+(s&0xffff); s+=(s>>16); return (unsigned short)~s;
}
// 1 si la passerelle repond en moins de 1,5 s par l'interface donnee.
static int icmp_probe(const char *iface, struct in_addr gw){
    int fd=socket(AF_INET,SOCK_RAW,IPPROTO_ICMP); if(fd<0) return 0;
    setsockopt(fd,SOL_SOCKET,SO_BINDTODEVICE,iface,strlen(iface)+1);
    static unsigned short seq=0; unsigned short id=(unsigned short)(getpid()&0xffff);
    unsigned char pkt[64]={0}; struct icmphdr *ic=(struct icmphdr*)pkt;
    ic->type=ICMP_ECHO; ic->un.echo.id=htons(id); ic->un.echo.sequence=htons(++seq);
    ic->checksum=0; ic->checksum=icmp_cksum(pkt,sizeof pkt);
    struct sockaddr_in to={0}; to.sin_family=AF_INET; to.sin_addr=gw;
    int ok=0;
    if(sendto(fd,pkt,sizeof pkt,0,(struct sockaddr*)&to,sizeof to)==(ssize_t)sizeof pkt){
        struct pollfd pf={fd,POLLIN,0}; struct timespec t0; clock_gettime(CLOCK_MONOTONIC,&t0);
        for(;;){
            struct timespec t1; clock_gettime(CLOCK_MONOTONIC,&t1);
            int left=1500-(int)((t1.tv_sec-t0.tv_sec)*1000+(t1.tv_nsec-t0.tv_nsec)/1000000);
            if(left<=0 || poll(&pf,1,left)<=0) break;
            unsigned char rb[256]; ssize_t n=recv(fd,rb,sizeof rb,0); if(n<=0) break;
            struct iphdr *ip=(struct iphdr*)rb; int hl=ip->ihl*4; if(n<hl+(int)sizeof(struct icmphdr)) continue;
            struct icmphdr *r=(struct icmphdr*)(rb+hl);
            if(r->type==ICMP_ECHOREPLY && ntohs(r->un.echo.id)==id){ ok=1; break; }
        }
    }
    close(fd); return ok;
}
static void *wifi_probe_thread(void *arg){
    (void)arg; int fails=0;
    for(;;){
        char iface[32]; struct in_addr gw; int st;
        if(!default_route(iface,sizeof iface,&gw)) { st=WIFI_DOWN; fails=0; }
        else if(strncmp(iface,"wl",2))              { st=WIFI_ETH;  fails=0; }
        else if(icmp_probe(iface,gw))               { st=WIFI_OK;   fails=0; }
        else { fails++; st = fails>=WIFI_FAILS_KO ? WIFI_KO : (g_wifi_state==WIFI_OK?WIFI_OK:WIFI_KO); }
        g_wifi_state=st;
        // en panne on resonde plus souvent pour voir revenir le lien vite
        sleep(st==WIFI_OK||st==WIFI_ETH ? WIFI_PROBE_S : 5);
    }
    return NULL;
}
static void wifi_probe_start(void){
    pthread_t th; pthread_attr_t at; pthread_attr_init(&at);
    pthread_attr_setdetachstate(&at,PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&at,64*1024);
    if(pthread_create(&th,&at,wifi_probe_thread,NULL)!=0) g_wifi_state=WIFI_UNKNOWN;
    pthread_attr_destroy(&at);
}
// Barre rouge en diagonale par-dessus l'icone : "pas de reseau".
static void draw_slash(uint32_t *buf,int W,int H,int x,int y,int w,int h){
    for(int i=0;i<w;i++){
        int yy=y+h-1-(int)((long)i*(h-1)/(w>1?w-1:1));
        for(int t=-1;t<=1;t++){ int px=x+i, py=yy+t; if(px<0||px>=W||py<0||py>=H) continue;
            blend_px(&buf[py*W+px],230,60,60,230); }
    }
}
// Parcourt /proc une fois par seconde : jeu en cours ? bluetoothd lance ?
static int g_btd_running = 0;
static int scan_in_game(void){
    DIR*d=opendir("/proc"); if(!d) return 0;
    struct dirent*e; int found=0, btd=0;
    while((e=readdir(d))){
        if(e->d_name[0]<'0'||e->d_name[0]>'9') continue;
        char p[64]; snprintf(p,sizeof p,"/proc/%s/comm",e->d_name);
        FILE*f=fopen(p,"r"); if(!f) continue;
        char c[32]={0}; if(fgets(c,sizeof c,f)){
            if(strncmp(c,"emulatorlaunche",15)==0||strncmp(c,"retroarch",9)==0) found=1;
            else if(strncmp(c,"bluetoothd",10)==0) btd=1;
        }
        fclose(f); if(found&&btd) break;
    }
    closedir(d); g_btd_running=btd; return found;
}

// Bluetooth : -1 = eteint (pas d'adaptateur, radio bloquee par rfkill ou
// bluetoothd arrete), 0 = actif sans connexion, 1 = au moins un appareil
// connecte. Le noyau cree une entree "hci0:<handle>" dans /sys/class/bluetooth
// pour chaque connexion : pas besoin de D-Bus.
enum { BT_OFF=-1, BT_ON=0, BT_CONN=1 };
static int read_bt_state(void){
    DIR*d=opendir("/sys/class/bluetooth"); if(!d) return BT_OFF;
    struct dirent*e; int adapter=0, conn=0;
    while((e=readdir(d))){
        if(strncmp(e->d_name,"hci",3)) continue;
        if(strchr(e->d_name,':')) conn=1; else adapter=1;
    }
    closedir(d);
    if(!adapter || !g_btd_running) return BT_OFF;
    d=opendir("/sys/class/rfkill");
    if(d){
        while((e=readdir(d))){
            if(e->d_name[0]=='.') continue;
            char p[300],t[32]; snprintf(p,sizeof p,"/sys/class/rfkill/%s/type",e->d_name);
            read_str_file(p,t,sizeof t); if(strcmp(t,"bluetooth")) continue;
            snprintf(p,sizeof p,"/sys/class/rfkill/%s/soft",e->d_name); int soft=read_int_file(p);
            snprintf(p,sizeof p,"/sys/class/rfkill/%s/hard",e->d_name); int hard=read_int_file(p);
            if(soft==1||hard==1){ closedir(d); return BT_OFF; }
        }
        closedir(d);
    }
    return conn?BT_CONN:BT_ON;
}
static void read_state(State *s){
    s->volume  = read_int_file("/sys/kernel/xpi_gamecon/volume");
    s->battery = read_int_file("/sys/class/power_supply/BAT0/capacity");
    if(s->battery<0) s->battery = read_int_file("/sys/kernel/xpi_gamecon/percent");
    char st[32]; read_str_file("/sys/class/power_supply/BAT0/status",st,sizeof st);
    s->charging = (strncmp(st,"Charging",8)==0);
    int t = read_int_file("/sys/class/thermal/thermal_zone0/temp");
    s->temp_c = (t>1000)?t/1000:t;
    s->cpu_pct = read_cpu_pct();
    s->wifi_bars = read_wifi_bars();
    s->wifi_state = g_wifi_state;
    s->bt = read_bt_state();
    time_t now=time(NULL); struct tm lt; localtime_r(&now,&lt);
    strftime(s->clock,sizeof s->clock,"%H:%M",&lt);
}
static const char *batt_bucket(int p){
    if(p<15) return "alert";
    static const int lv[]={20,30,50,60,80,90};
    for(int i=0;i<6;i++) if(p<=lv[i]+4){static char b[8];snprintf(b,8,"%d",lv[i]);return b;}
    return "full";
}
static void ensure_batt_icon(int p,int charging){
    char key[64]; snprintf(key,sizeof key,"%s%s",charging?"c":"",batt_bucket(p));
    if(strcmp(key,img_batt_key)==0) return;
    if(img_batt.rgba){stbi_image_free(img_batt.rgba);img_batt.rgba=NULL;}
    char path[600]; const char*bk=batt_bucket(p);
    if(charging) snprintf(path,sizeof path,"%s/ic_battery_charging_%s_white_18dp.png",res_dir,strcmp(bk,"alert")?bk:"20");
    else         snprintf(path,sizeof path,"%s/ic_battery_%s_white_18dp.png",res_dir,bk);
    img_batt=load_png(path); strcpy(img_batt_key,key);
}

// --------------------------------------------------------------- render ---
static double now_s(void){ struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts); return ts.tv_sec+ts.tv_nsec/1e9; }
static int last_volume=-1; static double volume_until=0; static int g_in_game=0;
static struct buf *get_free_buffer(int w,int h);

// dessine une pilule + icone + texte, renvoie la largeur totale occupee
static void widget(uint32_t *buf,int W,int H,int x,const Image *icon,const int *cp,int n,
                   int fixed_txt_w,uint8_t r,uint8_t g,uint8_t b){
    const int pad=8,iconG=4,iconY=(H-ICON_PX)/2,textY=(H-TEXT_PX)/2-1,pillY=(H-30)/2;
    int iw = icon?icon_w(icon):0, tw=fixed_txt_w, cw=(icon?iw+iconG:0)+tw;
    pill(buf,W,H,x-pad,pillY,cw+2*pad,30,150);
    if(icon) blit_icon(buf,W,H,icon,x,iconY);
    draw_text(buf,W,H,cp,n,x+(icon?iw+iconG:0),textY,r,g,b);
}
static int widget_w(const Image *icon,int fixed_txt_w){
    const int pad=8,iconG=4; int iw=icon?icon_w(icon):0;
    return (icon?iw+iconG:0)+fixed_txt_w+2*pad;
}

static void render(State *s){
    if(!configured||surf_w<=0) return;
    struct buf *b=get_free_buffer(surf_w,OSD_H); if(!b) return;
    int W=b->w,H=b->h; memset(b->px,0,b->size);

    if(!cfg_enabled){ wl_surface_attach(surface,b->wlb,0,0); wl_surface_damage_buffer(surface,0,0,W,H); wl_surface_commit(surface); b->busy=1; return; }

    int cp[40],n;
    const int textY=(H-TEXT_PX)/2-1, pillY=(H-30)/2;
    int ig = g_in_game;

    // ---- GAUCHE : temperature + cpu ----
    int show_temp = visible(m_temp,ig) && s->temp_c>0;
    int show_cpu  = visible(m_cpu,ig)  && s->cpu_pct>=0;
    if(show_temp||show_cpu){
        char txt[24];
        if(show_temp&&show_cpu) snprintf(txt,sizeof txt,"%d\xC2\xB0""C %d%%",s->temp_c,s->cpu_pct);
        else if(show_temp)      snprintf(txt,sizeof txt,"%d\xC2\xB0""C",s->temp_c);
        else                    snprintf(txt,sizeof txt,"%d%%",s->cpu_pct);
        n=str_cp_deg(txt,cp,40);
        int reserve = (show_temp&&show_cpu)?10 : show_temp?5 : 4;
        uint8_t rr=255,gg=255,bb=255; if(show_temp&&s->temp_c>=80){rr=255;gg=90;bb=60;}
        widget(b->px,W,H,10+8,&img_temp,cp,n,g_charw*reserve,rr,gg,bb);
    }

    // ---- DROITE : bluetooth + wifi + batterie dans UNE seule pilule ----
    // (meme principe que temperature + cpu a gauche ; batterie tout a droite)
    {
        Image *bi=NULL, *wi=NULL; int wifi_ko=0, show_batt=0, bn=0; int bcp[16];
        if(visible(m_bt,ig) && s->bt!=BT_OFF) bi = s->bt==BT_CONN ? &img_bt_conn : &img_bt;
        if(visible(m_wifi,ig)){
            int st=s->wifi_state;
            // Filaire porteur de la route par defaut : l'icone Wi-Fi n'a rien a dire.
            // Sonde indisponible (thread absent) : ancien comportement, barres brutes.
            int show_bars = (st==WIFI_OK || st==WIFI_UNKNOWN) && s->wifi_bars>=0;
            int show_ko   = (st==WIFI_KO) || (st==WIFI_DOWN && s->wifi_bars>=0);
            if(show_bars) wi=&img_wifi[s->wifi_bars>4?4:s->wifi_bars];
            // Icone vide + barre rouge : associe ou non, RIEN ne passe.
            else if(show_ko){ wi=&img_wifi[0]; wifi_ko=1; }
        }
        if(visible(m_battery,ig) && s->battery>=0){
            ensure_batt_icon(s->battery,s->charging);
            char txt[16]; snprintf(txt,sizeof txt,"%d%%",s->battery); bn=str_cp(txt,bcp,16);
            show_batt=1;
        }
        const int pad=8, gap=7, iconG=4, tw=g_charw*4;
        int cw=0, items=0;
        if(bi){ cw+=icon_w(bi); items++; }
        if(wi){ if(items) cw+=gap; cw+=icon_w(wi); items++; }
        if(show_batt){ if(items) cw+=gap; cw+=icon_w(&img_batt)+iconG+tw; items++; }
        if(items){
            int x=W-10-pad-cw, iy=(H-ICON_PX)/2;
            pill(b->px,W,H,x-pad,pillY,cw+2*pad,30,150);
            if(bi){ blit_icon(b->px,W,H,bi,x,iy); x+=icon_w(bi)+gap; }
            if(wi){
                blit_icon(b->px,W,H,wi,x,iy);
                if(wifi_ko) draw_slash(b->px,W,H,x-1,iy-1,icon_w(wi)+2,ICON_PX+2);
                x+=icon_w(wi)+gap;
            }
            if(show_batt){
                uint8_t rr=255,gg=255,bb=255; if(s->battery<15){rr=255;gg=80;bb=80;}
                blit_icon(b->px,W,H,&img_batt,x,iy);
                draw_text(b->px,W,H,bcp,bn,x+icon_w(&img_batt)+iconG,textY,rr,gg,bb);
            }
        }
    }

    // ---- CENTRE : message du service (prioritaire), sinon volume, sinon horloge ----
    // Fichier ecrit par piboy-dmgcontrol.py : ligne 1 = expiration (epoch),
    // ligne 2 = info|warn, ligne 3 = texte (court, ~22 caracteres).
    {
        static char msg[64]; static int msg_warn=0; static long msg_until=0; static time_t msg_mtime=0; static ino_t msg_ino=0;
        struct stat ms;
        // le service remplace le fichier (os.replace) : inode neuf a chaque message
        if(stat(MSG_PATH,&ms)==0 && (ms.st_mtime!=msg_mtime || ms.st_ino!=msg_ino)){
            msg_mtime=ms.st_mtime; msg_ino=ms.st_ino; msg_until=0;
            FILE*f=fopen(MSG_PATH,"r");
            if(f){ char l1[32]="",l2[16]="";
                if(fgets(l1,sizeof l1,f) && fgets(l2,sizeof l2,f) && fgets(msg,sizeof msg,f)){
                    char*nl=strchr(msg,'\n'); if(nl)*nl=0;
                    msg_until=atol(l1); msg_warn=!strncmp(l2,"warn",4); }
                fclose(f); }
        }
        if(msg_until && time(NULL)<msg_until && msg[0]){
            n=str_cp(msg,cp,40);
            int tw=g_charw*n, cw=widget_w(NULL,tw), x=(W-cw)/2+8;
            if(msg_warn) widget(b->px,W,H,x,NULL,cp,n,tw,255,110,80);
            else         widget(b->px,W,H,x,NULL,cp,n,tw,255,255,255);
            goto center_done;
        }
    }
    // ---- CENTRE : volume (transitoire) sinon horloge ----
    int vol_active = visible(m_volume,ig) && now_s()<volume_until && s->volume>=0;
    if(vol_active){
        int barW=140,barH=10,labelW=g_charw*3,tw=g_charw*3;
        char txt[8]; snprintf(txt,sizeof txt,"%d",s->volume); n=str_cp(txt,cp,8);
        int cw=labelW+6+barW+6+tw, x=(W-cw)/2;
        pill(b->px,W,H,x-8,pillY,cw+16,30,160);
        int vlbl[3]={'v','o','l'}; draw_text(b->px,W,H,vlbl,3,x,textY,255,255,255);
        int bx=x+labelW+6,by=(H-barH)/2;
        fill_rect(b->px,W,H,bx,by,barW,barH,255,255,255,60);
        fill_rect(b->px,W,H,bx,by,barW*s->volume/100,barH,255,255,255,230);
        draw_text(b->px,W,H,cp,n,bx+barW+6,textY,255,255,255);
    } else if(visible(m_clock,ig)){
        n=str_cp(s->clock,cp,8);
        int tw=g_charw*5, cw=widget_w(NULL,tw), x=(W-cw)/2+8;
        widget(b->px,W,H,x,NULL,cp,n,tw,255,255,255);
    }
center_done:

    wl_surface_attach(surface,b->wlb,0,0); wl_surface_damage_buffer(surface,0,0,W,H); wl_surface_commit(surface); b->busy=1;
}

// ----------------------------------------------------------- shm buffers ---
static void buf_release(void *data, struct wl_buffer *wlb){ (void)wlb; ((struct buf*)data)->busy=0; }
static const struct wl_buffer_listener buf_listener = { .release = buf_release };
static struct buf *get_free_buffer(int w,int h){
    for(int i=0;i<2;i++){ struct buf *b=&bufs[i]; if(b->busy) continue;
        if(b->wlb&&(b->w!=w||b->h!=h)){ wl_buffer_destroy(b->wlb); munmap(b->px,b->size); b->wlb=NULL; }
        if(!b->wlb){ int stride=w*4,size=stride*h; int fd=memfd_create("piboy-osd",MFD_CLOEXEC);
            if(fd<0||ftruncate(fd,size)<0){ if(fd>=0)close(fd); return NULL; }
            b->px=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
            struct wl_shm_pool*pool=wl_shm_create_pool(shm,fd,size);
            b->wlb=wl_shm_pool_create_buffer(pool,0,w,h,stride,WL_SHM_FORMAT_ARGB8888);
            wl_shm_pool_destroy(pool); close(fd);
            wl_buffer_add_listener(b->wlb,&buf_listener,b); b->w=w;b->h=h;b->size=size; }
        return b; }
    return NULL;
}

// --------------------------------------------------------- layer surface ---
static void ls_configure(void *d,struct zwlr_layer_surface_v1 *ls,uint32_t serial,uint32_t w,uint32_t h){
    (void)d;(void)h; if(w>0)surf_w=(int)w; zwlr_layer_surface_v1_ack_configure(ls,serial); configured=1;
}
static void ls_closed(void *d,struct zwlr_layer_surface_v1 *ls){ (void)d;(void)ls; running=0; }
static const struct zwlr_layer_surface_v1_listener ls_listener={.configure=ls_configure,.closed=ls_closed};
static void reg_global(void *d,struct wl_registry *r,uint32_t name,const char*iface,uint32_t v){
    (void)d;(void)v;
    if(!strcmp(iface,wl_compositor_interface.name)) compositor=wl_registry_bind(r,name,&wl_compositor_interface,4);
    else if(!strcmp(iface,wl_shm_interface.name)) shm=wl_registry_bind(r,name,&wl_shm_interface,1);
    else if(!strcmp(iface,zwlr_layer_shell_v1_interface.name)) layer_shell=wl_registry_bind(r,name,&zwlr_layer_shell_v1_interface,1);
}
static void reg_remove(void*d,struct wl_registry*r,uint32_t n){(void)d;(void)r;(void)n;}
static const struct wl_registry_listener reg_listener={.global=reg_global,.global_remove=reg_remove};

// ------------------------------------------------------------------ conf ---
static int parse_mode(const char *v){
    if(!strcmp(v,"off")||!strcmp(v,"0")) return MODE_OFF;
    if(!strcmp(v,"es")||!strcmp(v,"1"))  return MODE_ES;
    if(!strcmp(v,"game")||!strcmp(v,"jeu")||!strcmp(v,"2")) return MODE_GAME;
    return MODE_BOTH; // both/3/inconnu
}
static void load_conf(void){
    FILE*f=fopen(CONF_PATH,"r"); if(!f) return; char line[256];
    while(fgets(line,sizeof line,f)){
        char*p=line; while(*p==' '||*p=='\t')p++;
        if(*p=='#'||*p=='['||*p=='\n'||!*p) continue;
        char key[64],val[128];
        if(sscanf(p,"%63[^= ] = %127[^\n]",key,val)!=2 && sscanf(p,"%63[^=]=%127[^\n]",key,val)!=2) continue;
        char*e=val+strlen(val); while(e>val&&(e[-1]==' '||e[-1]=='\t'||e[-1]=='\r'))*--e=0;
        if(!strcmp(key,"enabled")) cfg_enabled=atoi(val);
        else if(!strcmp(key,"battery_mode")) m_battery=parse_mode(val);
        else if(!strcmp(key,"temp_mode"))    m_temp=parse_mode(val);
        else if(!strcmp(key,"cpu_mode"))     m_cpu=parse_mode(val);
        else if(!strcmp(key,"volume_mode"))  m_volume=parse_mode(val);
        else if(!strcmp(key,"wifi_mode"))    m_wifi=parse_mode(val);
        else if(!strcmp(key,"clock_mode"))   m_clock=parse_mode(val);
        else if(!strcmp(key,"bluetooth_mode")) m_bt=parse_mode(val);
        // compat ancienne syntaxe show_* (1=both,0=off)
        else if(!strcmp(key,"show_battery")) m_battery=atoi(val)?MODE_BOTH:MODE_OFF;
        else if(!strcmp(key,"show_temp"))    m_temp=atoi(val)?MODE_BOTH:MODE_OFF;
        else if(!strcmp(key,"show_cpu"))     m_cpu=atoi(val)?MODE_BOTH:MODE_OFF;
        else if(!strcmp(key,"show_volume"))  m_volume=atoi(val)?MODE_BOTH:MODE_OFF;
        else if(!strcmp(key,"volume_seconds")) VOLUME_SHOW_S=atof(val);
        else if(!strcmp(key,"position"))     cfg_bottom = (!strcmp(val,"bottom")||!strcmp(val,"bas"));
        else if(!strcmp(key,"height"))       OSD_H=atoi(val);
        else if(!strcmp(key,"text_size"))    TEXT_PX=atoi(val);
        else if(!strcmp(key,"icon_size"))    ICON_PX=atoi(val);
        else if(!strcmp(key,"resources"))    snprintf(res_dir,sizeof res_dir,"%s",val);
    }
    fclose(f);
}

int main(void){
    load_conf();
    display=wl_display_connect(NULL);
    if(!display){ fprintf(stderr,"piboy-osd: pas de Wayland (WAYLAND_DISPLAY=%s)\n",getenv("WAYLAND_DISPLAY")); return 1; }
    struct wl_registry *registry=wl_display_get_registry(display);
    wl_registry_add_listener(registry,&reg_listener,NULL);
    wl_display_roundtrip(display);
    if(!compositor||!shm||!layer_shell){ fprintf(stderr,"piboy-osd: interfaces manquantes (layer-shell absent ?)\n"); return 1; }

    load_font();
    char p[600];
    snprintf(p,sizeof p,"%s/ic_temperature_white_18dp.png",res_dir); img_temp=load_png(p);
    for(int i=0;i<5;i++){ snprintf(p,sizeof p,"%s/ic_signal_wifi_%d_bar_white_18dp.png",res_dir,i); img_wifi[i]=load_png(p); }
    snprintf(p,sizeof p,"%s/ic_bluetooth_white_18dp.png",res_dir); img_bt=load_png(p);
    snprintf(p,sizeof p,"%s/ic_bluetooth_connected_white_18dp.png",res_dir); img_bt_conn=load_png(p);
    wifi_probe_start();

    surface=wl_compositor_create_surface(compositor);
    struct wl_region *empty=wl_compositor_create_region(compositor);
    wl_surface_set_input_region(surface,empty); wl_region_destroy(empty);

    layer_surface=zwlr_layer_shell_v1_get_layer_surface(layer_shell,surface,NULL,ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY,"piboy-osd");
    zwlr_layer_surface_v1_set_size(layer_surface,0,OSD_H);
    zwlr_layer_surface_v1_set_anchor(layer_surface,
        (cfg_bottom?ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM:ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP)
        |ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT|ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    zwlr_layer_surface_v1_set_exclusive_zone(layer_surface,-1);
    zwlr_layer_surface_v1_set_keyboard_interactivity(layer_surface,0);
    zwlr_layer_surface_v1_add_listener(layer_surface,&ls_listener,NULL);
    wl_surface_commit(surface);
    wl_display_roundtrip(display);
    fprintf(stderr,"piboy-osd: demarre (largeur=%d h=%d)\n",surf_w,OSD_H);

    struct pollfd pfd={.fd=wl_display_get_fd(display),.events=POLLIN};
    read_cpu_pct();
    State st; double next_tick=0, next_ingame=0;
    struct stat cs; long conf_mtime=0; if(stat(CONF_PATH,&cs)==0) conf_mtime=cs.st_mtime;
    while(running){
        wl_display_flush(display);
        int pr=poll(&pfd,1,200);
        if(pr>0&&(pfd.revents&POLLIN)){ if(wl_display_dispatch(display)==-1) break; }
        else wl_display_dispatch_pending(display);
        double t=now_s();
        if(t>=next_ingame){ next_ingame=t+1.0; g_in_game=scan_in_game(); }
        if(t>=next_tick){
            next_tick=t+0.25;
            if(stat(CONF_PATH,&cs)==0&&cs.st_mtime!=conf_mtime){ conf_mtime=cs.st_mtime; load_conf(); }
            read_state(&st);
            if(st.volume!=last_volume){ if(last_volume>=0) volume_until=t+VOLUME_SHOW_S; last_volume=st.volume; }
            render(&st);
        }
    }
    wl_display_disconnect(display);
    return 0;
}
