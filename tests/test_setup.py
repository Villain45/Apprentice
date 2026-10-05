"""Manual Phase 0 smoke test. Run this file directly; pytest stays offline."""

MODEL = "ahmadwaqar/smolvlm2-agentic-gui:latest"

def grab(max_side=640):
    import io
    import mss
    from PIL import Image
    with mss.MSS() as sct:
        s = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", s.size, s.bgra, "raw", "BGRX")
    full = img.size
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue(), full, img.size

def main():
    import time
    import ollama

    png, full, small = grab()
    print("screen", full, "sent", small)
    for i in range(2):  # second run is the real number
        t = time.time()
        r = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": "Click the search box.", "images": [png]}],
            options={"num_predict": 64, "num_ctx": 2048}, keep_alive="30m",
        )
        print(f"run {i+1}: {time.time()-t:.1f}s ->", r["message"]["content"])


if __name__ == "__main__":
    main()
