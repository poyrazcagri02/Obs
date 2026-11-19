# OBS Not Takip

YTÜ OBS'ye ([obs.yildiz.edu.tr](https://obs.yildiz.edu.tr/oibs/std/)) girilen yeni notları Telegram'dan bildiren bot.
GitHub Actions üzerinde çalışır; bilgisayarının açık olmasına gerek yoktur.

- Yeni bir not girildiğinde ya da değiştiğinde mesaj atar: `📢 Matematik 1 – Vize 1: 78` ve altında tüm notlarının tablosu.
- İlk çalıştırmada derslerinin özetini gönderir, sonra yalnızca değişiklik olunca yazar.
- Giriş üst üste 3 kez başarısız olursa bir kez uyarır, düzelince bir kez haber verir.
- Şifren yalnızca kendi GitHub hesabındaki gizli ayarlarda (secrets) durur; kimseyle paylaşılmaz, log'a yazılmaz.
  Notların repoya kaydedilmez.

## Kurulum (yaklaşık 15 dakika, telefondan da yapılabilir)

Telefondan yapıyorsan GitHub sayfalarında tarayıcı menüsünden **"Masaüstü sitesi"**ni açman işini kolaylaştırır.

### 1. Telegram botu oluştur

1. Telegram'da **@BotFather**'a `/newbot` yaz, botuna bir ad ver.
   Verdiği `123456789:AA...` biçimindeki **token**'ı bir kenara not et.
2. Oluşturduğun botu aç ve **`/start`** yaz (bunu yapmazsan bot sana mesaj atamaz).
3. **@userinfobot**'a `/start` yaz; verdiği `Id` sayısı senin **chat ID**'n.
   (Bot token'ındaki `:` öncesi sayı DEĞİL, bu sayı.)

### 2. Repo

Bu repo ([poyrazcagri02/Obs](https://github.com/poyrazcagri02/Obs)), [brkyytkn10/obs-not-takip](https://github.com/brkyytkn10/obs-not-takip)
şablonundan kopyalandı; şablon adımını atlayabilirsin. Görünürlüğü **Settings → General → Danger Zone** altından değiştirebilirsin:
   - **Public (önerilen):** GitHub Actions ücretsiz ve sınırsız; 10 dakikada bir kontrol edebilirsin.
     Log'larda şifre ya da not yer almaz.
   - **Private:** Ayda 2000 dakika ücretsiz; her kontrol ~1-2 dakika sayıldığı için
     kontrolü **30 dakikada bir** yap (aşağıda 5. adım).

### 3. Gizli bilgileri ekle

Kendi repo'nda **Settings → Secrets and variables → Actions → New repository secret** ile şu dördünü ekle
(doğrudan link: https://github.com/poyrazcagri02/Obs/settings/secrets/actions/new):

| Name | Secret |
|---|---|
| `OBS_USER` | OBS kullanıcı adın (öğrenci numaran) |
| `OBS_PASS` | OBS şifren |
| `TG_TOKEN` | 1. adımdaki bot token'ı |
| `TG_CHAT_ID` | 1. adımdaki chat ID'n |

### 4. Dene

1. Repo'nda **Actions** sekmesine gir. Workflow'ları etkinleştirmeni isterse onayla.
2. Soldan **OBS Not Takip → Run workflow** → mode: **`test`** → Run workflow.
   1-2 dakika içinde Telegram'a "🧪 DENEME MESAJI" gelmeli.
3. Aynı şekilde mode: **`run`** ile çalıştır. Telegram'a derslerinin listesi gelmeli.

Gelmezse çalıştırmaya tıklayıp **Botu çalıştır** adımının log'una bak:
- `HTTP 403 ... can't send messages to the bot` → `TG_CHAT_ID` yanlış (1. adım, 3. madde).
- `HTTP 403 ... bot was blocked` ya da `chat not found` → botuna `/start` yazmamışsın.
- `Kimlik bilgisi hatası` → `OBS_USER` / `OBS_PASS` yanlış.

### 5. Otomatik kontrolü kur (cron-job.org)

GitHub'ın kendi zamanlayıcısı güvenilir olmadığı için kontrolü ücretsiz **cron-job.org** başlatır.

**a) GitHub'da kısıtlı bir token oluştur**

1. https://github.com/settings/personal-access-tokens/new
2. **Token name:** `obs-cron` · **Expiration:** en uzun süre (bitiş tarihini takvimine yaz)
3. **Repository access:** *Only select repositories* → `poyrazcagri02/Obs`
4. **Permissions → Repository permissions → Actions:** *Read and write* (başka hiçbir izin verme)
5. **Generate token** → çıkan `github_pat_...` değerini kopyala.

Bu token yalnızca bu repo'da botu çalıştırabilir; koduna, şifrene ya da secret'larına erişemez.

**b) cron-job.org'da görev oluştur**

1. https://cron-job.org → ücretsiz hesap aç → **Create cronjob**
2. **URL:** `https://api.github.com/repos/poyrazcagri02/Obs/actions/workflows/obs.yml/dispatches`
3. **Execution schedule:** Public repo için *Every 10 minutes*, private repo için *Every 30 minutes*
4. **Advanced** sekmesi:
   - **Request method:** `POST`
   - **Headers:**
     - `Authorization` → `Bearer github_pat_...` (a adımındaki token)
     - `Accept` → `application/vnd.github+json`
     - `Content-Type` → `application/json`
   - **Request body:** `{"ref":"main","inputs":{"mode":"run"}}`
     (`ref`, workflow dosyasının bulunduğu dal olmalı. Kod şu an `claude/gallant-cannon-3cbppa` dalında;
     `main`'e birleştirmediysen body'de `"ref":"claude/gallant-cannon-3cbppa"` yaz.)
5. **Test run** → **204 No Content** görmelisin. Sonra **Save**.

Bitti. Repo'nun **Actions** sekmesinde 10 (ya da 30) dakikada bir yeni çalıştırma görmelisin.

## Bakım

- **Token süresi dolunca** kontroller durur: 5a'daki gibi yeni token oluştur, cron-job.org'daki
  `Authorization` başlığını güncelle.
- **OBS şifreni değiştirirsen** `OBS_PASS` secret'ını güncelle.
- Captcha bazen okunamaz; bot her çalıştırmada 8 kez dener. Arada bir başarısız çalıştırma normaldir.

## Modlar

| Mod | Ne yapar |
|---|---|
| `run` | Giriş yapar, notları kontrol eder, değişiklik varsa mesaj atar (cron-job.org bunu çalıştırır). |
| `test` | OBS'ye girmeden örnek bir mesaj gönderir; Telegram ayarlarını denemek için. |
| `discover` | Geliştirme amaçlı: sayfaların HTML/ekran görüntülerini kaydeder. Yalnızca private repo'da artifact yükler. |
