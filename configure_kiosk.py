import sys
import re

def main():
    if len(sys.argv) < 5:
        print("Usage: python configure_kiosk.py <ip> <port> <key> <count>")
        sys.exit(1)
        
    ip = sys.argv[1]
    port = sys.argv[2]
    key = sys.argv[3]
    count = sys.argv[4]
    
    file_path = "kiosk_locker-3.py"
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        new_url = f"http://{ip}:{port}"
        content = re.sub(r'(TARGET_URL:\s*str\s*=\s*)"[^"]*"', f'TARGET_URL: str = "{new_url}"', content)
        content = re.sub(r'(UNLOCK_HOTKEY:\s*str\s*=\s*)"[^"]*"', f'UNLOCK_HOTKEY: str = "{key}"', content)
        content = re.sub(r'(UNLOCK_PRESS_COUNT:\s*int\s*=\s*)\d+', f'UNLOCK_PRESS_COUNT: int = {count}', content)
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        print(f"✅ Successfully configured {file_path}: URL={new_url}, Key={key}, PressCount={count}")
    except Exception as e:
        print(f"❌ Error configuring {file_path}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
