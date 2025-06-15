# Port 80 Kullanımı için Alternatifler

## Şu Anki Çözüm (Önerilen)
Dockerfile'da `setcap` kullanarak Python'a sadece sistem portlarına bağlanma yetkisi verilmiş:
```dockerfile
RUN setcap 'cap_net_bind_service=+ep' /usr/local/bin/python3.12
```

## Alternatif 1: Root Kullanıcısı
Eğer setcap çalışmazsa, Dockerfile'da şu değişikliği yapabilirsiniz:

```dockerfile
# Switch to app user first for file permissions
USER app

# Expose the SSE port  
EXPOSE 80

# Switch back to root for binding to port 80
USER root
```

## Alternatif 2: Docker Çalıştırma
Container'ı --privileged ile çalıştırın:
```bash
docker run --privileged -p 80:80 your-image
```

## Alternatif 3: Port Mapping
Container içinde farklı port kullanıp dışarıya 80 olarak açın:
```dockerfile
CMD ["--transport", "sse", "--sse-host", "0.0.0.0", "--sse-port", "8080"]
```
```bash
docker run -p 80:8080 your-image
``` 