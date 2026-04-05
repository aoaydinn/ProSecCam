# ProSecCam - Termux İçin Profesyonel Güvenlik Kamerası

ProSecCam, Android cihazınızdaki Termux ortamını kullanarak çalışan, gelişmiş hareket algılama yeteneklerine sahip profesyonel bir güvenlik kamerası uygulamasıdır. `Termux:API` özelliklerinden ve `OpenCV`'nin güçlü görüntü işleme yeteneklerinden faydalanır. 

Cihazınızı akıllı bir güvenlik kamerasına dönüştürerek, hareket algılandığında video kaydeder, fotoğraf çeker ve size Telegram veya SMS üzerinden anında bildirim gönderir.

## 🌟 Temel Özellikler

* **Akıllı Hareket Algılama:** OpenCV destekli arka plan çıkarma ve adaptif eşik (adaptive thresholding) yöntemleriyle yanlış alarmları en aza indirir.
* **Otomatik Kalibrasyon:** Başlangıçta ortamın aydınlatma ve gürültü durumuna göre kendi kendini kalibre eder.
* **Gelişmiş Pil Yönetimi (Battery Monitor):** Cihazın pil seviyesini ve sıcaklığını izler. Kritik seviyelere inildiğinde kendini beklemeye (pause) alır veya donanımı yormamak için çekim aralıklarını yavaşlatır. Sistem aşırı ısınırsa kendini korumaya alır.
* **Medya Kodlama:** Arka arkaya çekilen fotoğrafları (burst) ve senkronize sesi kullanarak `ffmpeg` ile mp4 formatında video oluşturur.
* **Akıllı Depolama Yönetimi (Storage Manager):** Diskinizin dolmasını önlemek için yaşlanan (örn. 7 günlük) veya belirli bir boyutu (örn. 500MB) aşan kayıtları otomatik olarak temizler.
* **Anlık Bildirimler:** Telegram (fotoğraflı/video mesaj), SMS ve yerel Termux bildirimleri ile sizi durumdan anında haberdar eder.
* **Gece Modu:** Karanlık ortamlarda flaşı (torch) otomatik olarak devreye sokar.
* **Esnek Yapılandırma:** Hem komut satırı (CLI) üzerinden hem de detaylı bir JSON konfigürasyon dosyası ile özelleştirilebilir.

---

## 🛠️ Kurulum Gereksinimleri

Bu projenin sorunsuz çalışabilmesi için telefonunuzda F-Droid veya benzeri bir kaynak üzerinden **Termux** ve **Termux:API** uygulamalarının yüklü ve gerekli izinlerin (Kamera, Depolama, Mikrofon, SMS vb.) cihaz ayarlarından Termux'a verilmiş olması gerekir.

### Adım Adım Kurulum

1. **Termux Ortamını Güncelleyin ve Bağımlılıkları Yükleyin:**
   Termux uygulamasını açın ve sırasıyla aşağıdaki komutları çalıştırarak gerekli temel Termux paketlerini kurun:
   ```bash
   pkg update && pkg upgrade
   pkg install git python python-numpy opencv 
   pkg install termux-api ffmpeg curl
   ```

2. **Termux Depolama İznini Verin:**
   Uygulamanın kayıtları cihazınızda düzgün arşivleyebilmesi için Termux'a depolama erişim izni vermelisiniz:
   ```bash
   termux-setup-storage
   ```
   *(Ekrana çıkan izin uyarısına onay verin.)*

3. **Projeyi GitHub'dan İndirin (Clone):**
   Git komutunu kullanarak projeyi cihazınıza indirin ve dizine geçiş yapın:
   ```bash
   git clone https://github.com/aoaydinn/ProSecCam.git
   cd ProSecCam
   ```

4. **Python Bağımlılıklarını Doğrulayın:**
   Zaten `pkg install` aşamasında arka planda `numpy` ve `opencv` kuruluyor ancak ek bir eksiklik olmasını önlemek adına dilerseniz requirements dosyasını da okutabilirsiniz:
   ```bash
   pip install -r requirements.txt
   ```

---

## 🚀 Kullanım

Projeyi hızlıca çalıştırmak için komut satırı argümanlarını veya yapılandırma dosyasını kullanabilirsiniz.

### 1. Varsayılan Yapılandırma Dosyası Oluşturma
Bütün ayarları detaylı görebileceğiniz bir `json` dosyası oluşturmak için:
```bash
python ProSecCam.py --init-config
```
Bu komut bulunduğunuz dizine `proseccam_config.json` dosyasını çıkartacaktır. Bu dosyayı düzenleyerek tüm sistem parametrelerini (çözünürlük, bekleme süresi, eşik değerleri) ayarlayabilirsiniz.

### 2. Uygulamayı Başlatma
Basitçe ön varsayılan ayarlar ile çalıştırmak:
```bash
python ProSecCam.py
```

Özel bir konfigürasyon dosyası kullanarak çalıştırmak:
```bash
python ProSecCam.py --config proseccam_config.json
```

### 3. Komut Satırı Argümanları ile Hızlı Başlatma
En sık kullanılan bazı ayarları dosyaya dokunmadan direkt komut satırından da belirtebilirsiniz:

* `--camera`: Kullanılacak kamera numarası (0=Arka, 1=Ön)
* `--night-mode`: Taranan veya kaydedilen karelerde flaş kullanılmasını aktif eder.
* `--telegram-token` ve `--telegram-chat`: Bildirim almak için Telegram bot detayları.
* `--sms`: Mesaj bildiriminin atılacağı telefon numarası.
* `--dry-run`: Sistemi test etmek içindir. Hareketi algılar bildirim verir ancak video kaydetmez (depolama alanını doldurmaz).
* `--no-audio`: Ses kaydını devre dışı bırakır.
* `--log-level`: Loglama seviyesi (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

**Örnek bir çalışma komutu:**
```bash
python ProSecCam.py --camera 1 --night-mode --telegram-token "YOUR_TOKEN" --telegram-chat "YOUR_CHAT_ID"
```

---

## ⚙️ Uygulama Durumları (State Machine)

ProSecCam bir durum makinesi (State Machine) mantığıyla çalışarak cihaz yorgunluğunu minimize eder ve stabiliteyi sağlar:

1. **INITIALIZING (Başlatılıyor):** Klasörlerin oluşturulması, kamera sensörünün testi ve ortam kalibrasyonu (baseline noise detection) yapılır. Termux uykuyu önleyici (wake lock) kilitleri aktif edilir.
2. **IDLE (Boşta):** Kamera belirtilen saniye aralıklarıyla (örn: 2.0 saniye) fotoğraf çeker ve hareketi analiz eder.
3. **DETECTING (Algılandı - Teyit):** Potansiyel hareket algılandığında arka arkaya hızlı teyit kareleri yakalar. Gerçekten hareket varsa doğrular.
4. **RECORDING (Kaydediliyor):** Kesin hareket doğrulandığında seri fotoğraf çekimine ve ses kaydına başlar, ardından videoyu oluşturur ve uyarı mesajı (Telegram/SMS) atar.
5. **COOLDOWN (Soğuma):** Bir olayı kaydettikten sonra belirli bir saniye "sağır" şekilde bekler. Bu art arda spam kayıtlarını önler.
6. **LOW_BATTERY / PAUSED:** Telefon şarjı belli bir yüzdenin altındaysa sistemi korumaya alır. Görüntü işleme sıklığını çok ciddi oranda düşürür veya %10 kritik seviyede sistemi tamamen duraklatarak (PAUSED) şarja takılmasını bekler. Güvenli bölgeye şarj edilince tekrar **IDLE** durumuna döner.

---

## 📂 Depolama Klasör Yapısı

Sistem kayıtları ve işlemleri varsayılan olarak cihazınızda şu adreste toplar: `/data/data/com.termux/files/home/proseccam` (Yapılandırma dosyasından değiştirilebilir).

* `/events`: Algılanan her bir olay alt klasörler şeklinde (`YYYYMMDD_HHMMSS`) tutulur. İçerisinde mp4 video, trigger fotoğrafı, meta bilgiler, ses dosyası bulunur.
* `/temp`: Hızlı işlenme ve yakalama süreçleri için geçici bellek bölgesidir.
* `/logs`: Sistem hata kayıtlarının ve log rotasyonlarının tutulduğu `proseccam.log` dosyalarını içerir.

---

## ⚠️ Önemli Notlar ve İpuçları
* **İzinler:** Arka planda problemsiz çalışması için Termux'a pil tasarruf kısıtlamalarının **uygulanmaması** ve uykudayken dahi çalışabilmesi için Android ayarlarından izin gereklidir.
* **Gece Modu:** Ön kamerada flaş bulunmadığı cihazlarda `--camera 1 --night-mode` yapılması `termux-torch` uygulamasında hataya yol açabilir.
* **FFmpeg:** Video yapımı (Encoding) CPU ağırlıklı bir işlemdir. `--dry-run` u kullanarak video kaydedilmeden de sisteminizin hareketi ne kadar iyi yakaladığını test edebilirsiniz. 
* Kapatmak için Termux üzerinden `Ctrl + C` tuş kombinasyonuyla uygulamanın temiz ve güvenli bir şekilde `SHUTTING_DOWN` (Kapatma) sürecine girmesini sağlayabilirsiniz. Böylece kalan artık geçici dosyalar ve açık mikrofon gibi kanallar da silinir / kapatılır.
