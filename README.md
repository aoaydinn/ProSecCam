# ProSecCam v2.0 - Termux İçin Profesyonel Güvenlik Kamerası

ProSecCam, Android cihazınızdaki Termux ortamını kullanarak çalışan, gelişmiş hareket algılama yeteneklerine sahip profesyonel bir güvenlik kamerası uygulamasıdır. `Termux:API` özelliklerinden ve `OpenCV`'nin güçlü görüntü işleme yeteneklerinden faydalanır. 

Cihazınızı akıllı bir güvenlik kamerasına dönüştürerek, hareket algılandığında video kaydeder, fotoğraf çeker ve size Telegram veya SMS üzerinden anında bildirim gönderir.

## 🌟 Temel Özellikler

* **Akıllı Hareket Algılama:** OpenCV destekli arka plan çıkarma ve adaptif eşik yöntemleriyle yanlış alarmları en aza indirir.
* **Otomatik Kalibrasyon:** Başlangıçta ortamın aydınlatma ve gürültü durumuna göre kendi kendini kalibre eder.
* **Gelişmiş Pil ve Sıcaklık Yönetimi:** Cihazın pil seviyesini ve sıcaklığını izler. Kritik seviyelere inildiğinde kendini beklemeye alır veya çekim aralıklarını yavaşlatır. Sistemin aşırı ısınmasını engeller.
* **Medya Kodlama:** Arka arkaya çekilen fotoğrafları (burst) ve senkronize sesi kullanarak `ffmpeg` ile (varsayılan 60 saniyelik minimum) mp4 video (sesli) oluşturur.
* **Akıllı Depolama Yönetimi:** Diskinizin dolmasını önlemek için yaşlanan (örn. 7 günlük) veya belirli bir boyutu (örn. 500MB) aşan kayıtları otomatik olarak temizler.
* **Anlık Bildirimler:** Telegram (fotoğraflı/video mesaj), SMS ve yerel Termux bildirimleri ile sizi durumdan anında haberdar eder.
* **Gece Modu:** Karanlık ortamlarda ve gece çekimlerinde flaşı (torch) otomatik olarak devreye sokar.
* **Otomatik Kurulum (Auto-Installer):** Komut satırından tek komutla tüm bağımlılıkların otomatik kurulmasını ve gerekli izinlerin ayarlanmasını sağlar.
* **Esnek Yapılandırma:** JSON konfigürasyon dosyası ve komut satırı argümanları ile tüm parametreler özelleştirilebilir.

---

## 🛠️ Kurulum Gereksinimleri

Bu projenin sorunsuz çalışabilmesi için telefonunuzda **F-Droid** üzerinden **Termux** ve **Termux:API** uygulamalarının yüklü olması gerekir (Google Play sürümleri güncel değildir, lütfen F-Droid kullanın).

Ayrıca Android cihaz ayarlarından (Ayarlar > Uygulamalar > Termux:API > İzinler) uygulamanızın Kamera, Mikrofon ve Depolama izinleri olmak zorundadır.

### Adım Adım Kurulum

1. **Projeyi cihazınıza indirin ve dizine geçiş yapın:**
   ```bash
   pkg install git
   git clone https://github.com/aoaydinn/ProSecCam.git
   cd ProSecCam
   ```

2. **Otomatik Kurulum aracını çalıştırın:**
   Sistemi ortamınıza göre hazırlamak, tüm bağımlılıkları (Python, OpenCV, ffmpeg vb.) tek tıkla yüklemek ve izinleri ayarlamak için aşağıdaki komutu kullanın:
   ```bash
   python ProSecCam.py --setup
   ```

*(Bu işlem internet bağlantı hızınıza ve cihazınıza göre `opencv` kurulumu sebebiyle birkaç dakika sürebilir. Cihaz ekranında çıkan depolama izni onayı gibi uyarıları kabul ediniz.)*

---

## 🚀 Kullanım

Kurulum tamamlandıktan sonra uygulamayı aşağıdaki şekillerde başlatabilirsiniz:

### 1. Sistem Yapılandırması (Config) Oluşturma
Bütün ayarları detaylı görebileceğiniz bir `json` dosyası oluşturmak için:
```bash
python ProSecCam.py --init-config
```
Bu komut `proseccam_config.json` dosyasını oluşturur. Buradan depolama sınırları, kayıt aralığı, hareket eşik değerleri, pil koruma modları ve bildirim (Telegram/SMS) gibi ayarlarınızı detaylı düzenleyebilirsiniz.

### 2. Uygulamayı Başlatma
Varsayılan veya kayıtlı `json` ayarlarınız ile güvenlik kamerasını doğrudan başlatmak için:
```bash
python ProSecCam.py
```

### 3. Komut Satırı Argümanları ile Kullanım
Belirli ayarları `.json` dosyasına dokunmadan komut satırından da parametre olarak gönderebilirsiniz:

* `--setup`: İlk kullanım kurulumunu gerçekleştirir.
* `--init-config`: Varsayılan ayar dosyasını (`proseccam_config.json`) oluşturur.
* `--dry-run`: Sistemi test etmek içindir. Hareketi algılar ancak video kaydetmez ve bildirim yapmaz (test/geliştirme modu).
* `--camera`: Kullanılacak kamera sensör numarası (Örn. `--camera 1` ön kamera).
* `--night-mode`: Video çekiminde flaşı dahil eder.
* `--no-audio`: Video çekimlerinde ses algılamayı kapatır.
* `--telegram-token` ve `--telegram-chat`: Hareket anında bildirim almak için Telegram config bilgileri.
* `--sms`: Güvenlik ihlali anında SMS atılacak numara.

**Örnek bir çalışma komutu:**
```bash
python ProSecCam.py --camera 1 --night-mode --telegram-token "TOKEN" --telegram-chat "CHAT_ID"
```

---

## ⚠️ Önemli Android Ayarları (Stabilite İçin Zorunlu)

Android cihazların arkaplanda bekleyen sistemi kapatmasını önlemek için bazı ayarların yapılması **zorunludur**:

### 1. Pil Optimizasyonunu Kapatma
Termux'un ekran kapalıyken veya arka planda sorunsuz çalışması için:
* `Ayarlar` > `Uygulamalar` > `Termux` > `Pil` altındaki optimizasyon kısıtlamalarını **Kısıtlama Yok** pozisyonuna ayarlayın.
* Aynısını `Termux:API` uygulaması için de tekrarlayın.
* Uygulama açıkken yukarıdaki Termux bildirim çubuğunda yer alan "**ACQUIRE WAKELOCK**" tuşuna basarak uykuyu önleyin.

### 2. Phantom Process Killer (Android 12+ Cihazlar İçin)
Android 12 ve üzeri işletim sistemleri uzun süren Termux (ffmpeg gibi) işlemlerini aniden öldürebilir.
* **Android 14+ İçin Kolay Çözüm:** 
  Cihaz `Geliştirici Seçenekleri`ne gidin ve "Alt işlem kısıtlamalarını devre dışı bırak" (Disable child process restrictions) seçeneğini bularak aktif edin.
* **Android 12-13 İçin Çözüm (ADB gerektirir):**
  Sorunu PC'den `adb shell` komutları göndererek temelli kaldırabilirsiniz:
  ```bash
  adb shell "/system/bin/device_config set_sync_disabled_for_tests persistent"
  adb shell "/system/bin/device_config put activity_manager max_phantom_processes 2147483647"
  adb shell settings put global settings_enable_monitor_phantom_procs false
  ```

---

## ⚙️ Uygulama Durumları (State Machine)

ProSecCam stabil kalabilmek için bir durum makinesi mantığı kullanır:

1. **INITIALIZING (Başlatılıyor):** Gerekli dosya/klasör yapıları hazırlanır, kamera/Termux yetkileri test edilir, kalibrasyon (aydınlık/parlaklık ölçümü) yapılır.
2. **IDLE (Boşta):** Sistem çok düşük güç tüketerek belirtilen aralıklar ile hareket olup olmadığını inceler.
3. **DETECTING (Teyit):** Potansiyel hareket hissedildiğinde hızlı frekansta peş peşe kareler çekilerek doğrulanır.
4. **RECORDING (Kaydediliyor):** Hareket teyit edildiği anda olay kaydedilmeye başlanır, ses+fotoğraflar bir mp4 olur, ve gerekli bildirimler fırlatılır.
5. **COOLDOWN (Soğuma):** Aynı hareket devam bile etse aşırı ısınma ve spam eylemleri önlemek için araya boş bir mola (Örn: 15 sn) konulur.
6. **LOW_BATTERY / PAUSED:** Telefon pili %25 veya %10 kritik seviyeye kadar düşerse sistem ya frekansı düşürür ya da cihaz fişe takılana kadar tamamen PAUSED moda geçerek pil koruması yapar. Aynı durum max_temperature geçildiğinde de uygulanır.

## 📂 Depolama Klasör Yapısı

Sistem kayıtları, olay anındaki videolar veya test fotoğrafları şu yolda tutulmaktadır: 
`/data/data/com.termux/files/home/proseccam` (Dilenirse yapılandırma dosyasından değiştirilebilir).

* `/events`: Algılanan olayların saat ve tarihine (`YYYYMMDD_HHMMSS`) göre tutulduğu asıl kanıt klasörü.
* `/temp`: Sistemin geçici video işleme operasyonları için bellekte tutulan dosyalar.
* `/logs`: Hata ayıklama veya çalışma notları (`proseccam.log`) dosyası.
