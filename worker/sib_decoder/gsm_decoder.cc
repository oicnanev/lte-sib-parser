// gsm_decoder: every GSM BCCH in a wide capture file -> one JSON line per cell.
//
// C++ port of vol/scripts/gsm_decode.py (same algorithms, same output keys), ~10x
// faster: FFT filter bank (FFTW, single precision) into 200 kHz channels at 2
// samples per symbol, FCCH (differential detector + FCCH frame distances), SCH
// (two must agree), BCCH blocks (least squares channel estimate, max-log BCJR,
// soft Viterbi, Fire code). The SI messages are printed as hex with their TC;
// gsm_decode.py parses them.
//
//   gsm_decoder -i file.iq -f centre_hz -r rate [-w bw] [-a "arfcn ..."] [--int8]
//               [--secs S] [--presearch S] [--fcch-max-hz F] [-j threads]
#include <fftw3.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <utility>
#include <mutex>
#include <numeric>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

typedef std::complex<float> cf;
typedef std::complex<double> cd;

// vector whose resize() leaves new elements uninitialised: channel buffers are
// hundreds of MB, and zeroing them in one thread cost more than filling them
template <class T>
struct NoInit : std::allocator<T> {
  template <class U>
  struct rebind {
    typedef NoInit<U> other;
  };
  NoInit() = default;
  template <class U>
  NoInit(const NoInit<U>&) {}
  template <class U>
  void construct(U*) noexcept {}
  template <class U, class... A>
  void construct(U* p, A&&... a) { ::new ((void*)p) U(std::forward<A>(a)...); }
};
typedef std::vector<cf, NoInit<cf>> Stream;

// exp(-j 2 pi f n) by recurrence, renormalised now and then (no sin/cos per sample)
struct Phasor {
  cd v = 1, step;
  long k = 0;
  explicit Phasor(double cycles_per_sample) : step(std::cos(-2 * M_PI * cycles_per_sample), std::sin(-2 * M_PI * cycles_per_sample)) {}
  cd next()
  {
    cd r = v;
    v *= step;
    if ((++k & 1023) == 0) v /= std::abs(v);
    return r;
  }
};

static const double R = 1625000.0 / 6;  // symbol rate
static const int SPS = 2;
static const double FSC = R * SPS;
static const int FRAME = 1250 * SPS;
static const long HYPER = 2715648;
static const int BURST = 148;
static double FCCH_MAX_HZ = 25e3;

static const char* SCH_TRAIN = "1011100101100010000001000000111100101101010001010111011000011011";
static const char* TSC[8] = {"00100101110000100010010111", "00101101110111100010110111", "01000011101110100100001110",
                             "01000111101101000100011110", "00011010111001000001101011", "01001110101100000100111010",
                             "10100111110110001010011111", "11101111000100101110111100"};

static double arfcn_freq(int a)
{
  if (a >= 512 && a <= 885) return 1805.2e6 + 0.2e6 * (a - 512);
  if (a >= 975 && a <= 1023) return 935.0e6 + 0.2e6 * (a - 1024);
  return 935.0e6 + 0.2e6 * a;
}

// ------------------------------------------------------------------ capture file

struct Capture {
  const uint8_t* data = nullptr;
  size_t bytes = 0;
  bool int8 = false;
  long n = 0;  // complex samples
  double full = 2048;
  bool open(const char* path, bool i8)
  {
    int fd = ::open(path, O_RDONLY);
    if (fd < 0) return false;
    struct stat st;
    fstat(fd, &st);
    bytes = st.st_size;
    data = (const uint8_t*)mmap(nullptr, bytes, PROT_READ, MAP_PRIVATE, fd, 0);
    ::close(fd);
    if (data == MAP_FAILED) return false;
    int8 = i8;
    full = i8 ? 128 : 2048;
    n = bytes / (i8 ? 2 : 4);
    return true;
  }
  void read(long start, long count, cf* out) const
  {
    if (int8) {
      const int8_t* p = (const int8_t*)data + 2 * start;
      for (long i = 0; i < count; i++) out[i] = cf(p[2 * i], p[2 * i + 1]);
    } else {
      const int16_t* p = (const int16_t*)data + 2 * start;
      for (long i = 0; i < count; i++) out[i] = cf(p[2 * i], p[2 * i + 1]);
    }
  }
};

// ------------------------------------------------------------------ filter bank

static long gcdl(long a, long b) { return b ? gcdl(b, a % b) : a; }

struct Plan {
  long B, M, S, MS, E;
};

static Plan make_plan(double fs, int target = 1000)
{
  // B/M = fs/FSC exactly = fs*6 / (1625000*2); k a power of two (fast FFT sizes)
  long num = (long)llround(fs) * 6, den = 1625000L * SPS;
  long g = gcdl(num, den);
  num /= g;
  den /= g;
  int e = std::max(4, (int)lround(std::log2((double)target / den)));
  long k = 1L << e;
  Plan p;
  p.B = num * k;
  p.M = den * k;
  p.S = p.B - p.B / 8;  // overlap-save, 1/8 overlap
  p.MS = p.M - p.M / 8;
  p.E = p.M / 16;
  return p;
}

// channel streams for `arfcns` over `secs` seconds (0: whole file) of the capture
static std::map<int, Stream> channelize(const Capture& cap, double fs, double fc, double bw,
                                                 const std::vector<int>& arfcns, double secs, int threads)
{
  Plan p = make_plan(fs);
  long start0 = (long)(fs * 0.01);
  long n = cap.n;
  if (secs > 0) n = std::min(n, start0 + (long)(fs * secs) + p.B);
  long nblk = (n - start0 - p.B) / p.S + 1;
  std::map<int, Stream> out;
  if (nblk <= 0) return out;
  double df = fs / p.B;
  // extracted bins in FFT order: k = 0..M/2-1, -M/2..-1
  // only the bins inside the filter (about half of the M) are gathered
  std::vector<float> W;
  std::vector<long> kk, ii;
  for (long i = 0; i < p.M; i++) {
    long k = i < p.M / 2 ? i : i - p.M;
    double af = std::fabs(k * df);
    double w = af < 80e3 ? 1.0 : af < 130e3 ? 0.5 + 0.5 * std::cos(M_PI * (af - 80e3) / 50e3) : 0.0;
    if (w == 0) continue;
    ii.push_back(i);
    kk.push_back(k);
    W.push_back((float)(w / (p.B * cap.full)));  // FFTW is unnormalised; 1.0 = full scale
  }
  size_t nw = W.size();
  struct Ch {
    int a;
    long c0;
    Stream* y;
  };
  std::vector<Ch> chans;
  for (int a : arfcns) {
    double off = arfcn_freq(a) - fc;
    if (std::fabs(off) > bw / 2 - 150e3) continue;
    auto& v = out[a];
    v.resize(nblk * p.MS);
    chans.push_back({a, lround(off / df), &v});
  }
  if (chans.empty()) return out;

  fftwf_complex* tb = fftwf_alloc_complex(p.B);
  fftwf_complex* tm = fftwf_alloc_complex(p.M);
  fftwf_plan fwd = fftwf_plan_dft_1d(p.B, tb, tb, FFTW_FORWARD, FFTW_ESTIMATE);
  fftwf_plan inv = fftwf_plan_dft_1d(p.M, tm, tm, FFTW_BACKWARD, FFTW_ESTIMATE);
  std::atomic<long> next(0);
  auto work = [&]() {
    fftwf_complex* X = fftwf_alloc_complex(p.B);
    fftwf_complex* Y = fftwf_alloc_complex(p.M);
    for (long b; (b = next++) < nblk;) {
      cap.read(start0 + b * p.S, p.B, (cf*)X);
      fftwf_execute_dft(fwd, X, X);
      const cf* Xc = (const cf*)X;
      cf* Yc = (cf*)Y;
      for (auto& c : chans) {
        std::memset(Y, 0, sizeof(fftwf_complex) * p.M);
        long base = ((c.c0 % p.B) + p.B) % p.B;
        for (size_t j = 0; j < nw; j++) {
          long idx = base + kk[j];
          if (idx < 0)
            idx += p.B;
          else if (idx >= p.B)
            idx -= p.B;
          Yc[ii[j]] = Xc[idx] * W[j];
        }
        fftwf_execute_dft(inv, Y, Y);
        // block b starts at b*S: rotate its phase onto the common time base
        double ph = -2 * M_PI * (double)c.c0 * (double)((b * p.S) % p.B) / p.B;
        cf rot((float)std::cos(ph), (float)std::sin(ph));
        cf* dst = c.y->data() + b * p.MS;
        for (long i = 0; i < p.MS; i++) dst[i] = Yc[p.E + i] * rot;
      }
    }
    fftwf_free(X);
    fftwf_free(Y);
  };
  std::vector<std::thread> th;
  for (int t = 0; t < threads; t++) th.emplace_back(work);
  for (auto& t : th) t.join();
  fftwf_destroy_plan(fwd);
  fftwf_destroy_plan(inv);
  fftwf_free(tb);
  fftwf_free(tm);
  return out;
}

// ------------------------------------------------------------------ FCCH

static std::mutex fftw_plan_lock;  // FFTW planning is not thread safe

static std::vector<long> fcch_pattern(const std::vector<long>& hits, int tol = 40, double frac = 0.4)
{
  static const int K[] = {10, 11, 20, 21, 30, 31, 40, 41, 51};
  std::set<size_t> ok;
  for (size_t i = 0; i < hits.size(); i++)
    for (size_t j = i + 1; j < hits.size(); j++) {
      long d = hits[j] - hits[i];
      long k = lround((double)d / FRAME);
      if (k > 51) break;
      if (std::labs(d - k * FRAME) <= tol && std::find(std::begin(K), std::end(K), k) != std::end(K)) {
        ok.insert(i);
        ok.insert(j);
      }
    }
  std::vector<long> out;
  if (ok.size() < 3 || ok.size() < frac * hits.size()) return out;
  for (size_t i : ok) out.push_back(hits[i]);
  return out;
}

// exp(-j 2 pi n / 8): the FCCH tone (R/4) and the GMSK pi/2 per symbol at 2 SPS
static const cf ROT8[8] = {cf(1, 0), cf(M_SQRT1_2, -M_SQRT1_2), cf(0, -1), cf(-M_SQRT1_2, -M_SQRT1_2),
                           cf(-1, 0), cf(-M_SQRT1_2, M_SQRT1_2), cf(0, 1), cf(M_SQRT1_2, M_SQRT1_2)};

// start samples of FCCH bursts and the frequency offset (Hz)
static std::vector<long> find_fcch(const Stream& z, double* dfo)
{
  long N = z.size();
  *dfo = 0;
  std::vector<cf> w(N);
  for (long i = 0; i < N; i++) w[i] = z[i] * ROT8[i & 7];
  const int L = 250;
  // coherence of d[n] = w[n+1] w*[n] over L samples (real part: phase ~0)
  std::vector<float> coh(std::max(0L, N - L));
  {
    double sr = 0, sa = 0;
    auto dd = [&](long k) { return w[k + 1] * std::conj(w[k]); };
    for (long k = 0; k < L && k + 1 < N; k++) {
      cf d = dd(k);
      sr += d.real();
      sa += std::abs(d);
    }
    for (long k = 0; k + L < N; k++) {
      coh[k] = (float)(sr / (sa + 1e-30));
      cf a = dd(k), b = dd(k + L);
      sr += b.real() - a.real();
      sa += std::abs(b) - std::abs(a);
    }
  }
  std::vector<long> hits;
  long nc = coh.size();
  for (long i = 0; i < nc;) {
    long j = i;
    while (j < nc && coh[j] <= 0.85f) j++;
    if (j >= nc) break;
    long e = std::min(nc, j + 400);
    float top = *std::max_element(coh.begin() + j, coh.begin() + e);
    double sum = 0;
    long cnt = 0;
    for (long k = j; k < e; k++)
      if (coh[k] > top * 0.97f) {
        sum += k - j;
        cnt++;
      }
    long mid = j + (long)(sum / cnt);
    hits.push_back(mid + L / 2 - BURST * SPS / 2);
    i = j + 4000;
  }
  hits = fcch_pattern(hits);
  if (hits.empty()) return hits;

  // frequency offset: FFT peak of the tone around each FCCH
  const int NF = 8192;
  fftwf_complex* buf = fftwf_alloc_complex(NF);
  fftwf_plan pl;
  {
    std::lock_guard<std::mutex> g(fftw_plan_lock);
    pl = fftwf_plan_dft_1d(NF, buf, buf, FFTW_FORWARD, FFTW_ESTIMATE);
  }
  std::vector<double> offs;
  for (long s : hits) {
    long a = std::max(0L, s - 150), b = std::min(N, s + BURST * SPS + 150);
    std::memset(buf, 0, sizeof(fftwf_complex) * NF);
    for (long k = a; k < b && k - a < NF; k++) ((cf*)buf)[k - a] = w[k];
    fftwf_execute(pl);
    double best = -1, bf = 0;
    for (int k = 0; k < NF; k++) {
      double f = (k < NF / 2 ? k : k - NF) * FSC / NF;
      if (std::fabs(f) >= FCCH_MAX_HZ) continue;
      double m = std::norm(((cf*)buf)[k]);
      if (m > best) {
        best = m;
        bf = f;
      }
    }
    offs.push_back(bf);
  }
  {
    std::lock_guard<std::mutex> g(fftw_plan_lock);
    fftwf_destroy_plan(pl);
  }
  fftwf_free(buf);
  std::vector<double> so = offs;
  std::sort(so.begin(), so.end());
  double df = so.size() % 2 ? so[so.size() / 2] : 0.5 * (so[so.size() / 2 - 1] + so[so.size() / 2]);
  *dfo = df;
  // burst start: coherent sum of the corrected tone over one burst
  std::vector<cd> cv(N + 1);
  cv[0] = 0;
  Phasor ph(df / FSC);
  for (long k = 0; k < N; k++) cv[k + 1] = cv[k] + cd(w[k]) * ph.next();
  const long Lb = BURST * SPS;
  std::vector<long> starts;
  for (long s : hits) {
    long lo = std::max(0L, s - 150), hi = std::min(N - Lb, s + 150);
    if (hi <= lo) continue;
    long bk = lo;
    double bm = -1;
    for (long k = lo; k < hi; k++) {
      double m = std::abs(cv[k + Lb] - cv[k]);
      if (m > bm) {
        bm = m;
        bk = k;
      }
    }
    starts.push_back(bk);
  }
  return starts;
}

// ------------------------------------------------------------------ burst demodulation

static const int LTAP = 5, DLY = 2;
static inline double sym(int b) { return 1 - 2 * b; }

struct Est {
  std::vector<int> obs;
  std::vector<double> A;  // [nobs][5]
  std::vector<double> P;  // [5][nobs] pseudo-inverse
};

static Est make_est(const char* train, int tpos)
{
  Est e;
  int len = strlen(train);
  for (int n = tpos + DLY; n < tpos + len - (LTAP - 1 - DLY); n++) e.obs.push_back(n);
  int no = e.obs.size();
  e.A.resize(no * LTAP);
  for (int r = 0; r < no; r++)
    for (int k = 0; k < LTAP; k++) e.A[r * LTAP + k] = sym(train[e.obs[r] + DLY - k - tpos] - '0');
  // P = (A^T A)^-1 A^T
  double G[LTAP][2 * LTAP] = {};
  for (int i = 0; i < LTAP; i++) {
    for (int j = 0; j < LTAP; j++)
      for (int r = 0; r < no; r++) G[i][j] += e.A[r * LTAP + i] * e.A[r * LTAP + j];
    G[i][LTAP + i] = 1;
  }
  for (int c = 0; c < LTAP; c++) {
    int piv = c;
    for (int r = c + 1; r < LTAP; r++)
      if (std::fabs(G[r][c]) > std::fabs(G[piv][c])) piv = r;
    for (int j = 0; j < 2 * LTAP; j++) std::swap(G[c][j], G[piv][j]);
    double d = G[c][c];
    for (int j = 0; j < 2 * LTAP; j++) G[c][j] /= d;
    for (int r = 0; r < LTAP; r++)
      if (r != c) {
        double f = G[r][c];
        for (int j = 0; j < 2 * LTAP; j++) G[r][j] -= f * G[c][j];
      }
  }
  e.P.assign(LTAP * no, 0);
  for (int i = 0; i < LTAP; i++)
    for (int r = 0; r < no; r++) {
      double s = 0;
      for (int j = 0; j < LTAP; j++) s += G[i][LTAP + j] * e.A[r * LTAP + j];
      e.P[i * no + r] = s;
    }
  return e;
}

static int P_[16][2], SUCC[16][2], XOLD[16];
static double SYMTAB[16][2][LTAP];

static void init_tables()
{
  for (int ns = 0; ns < 16; ns++) {
    P_[ns][0] = ns >> 1;
    P_[ns][1] = (ns >> 1) | 8;
    for (int x = 0; x < 2; x++) {
      int old = (ns >> 1) | (x << 3);
      SYMTAB[ns][x][0] = sym(ns & 1);
      for (int j = 0; j < 4; j++) SYMTAB[ns][x][j + 1] = sym((old >> j) & 1);
    }
  }
  for (int o = 0; o < 16; o++) {
    SUCC[o][0] = (o << 1) & 15;
    SUCC[o][1] = ((o << 1) & 15) | 1;
    XOLD[o] = o >> 3;
  }
}

// max-log BCJR over one burst: soft bits (> 0 means bit 0)
static void mlse(const cd* r, const cd* h, double sigma2, double* llr)
{
  cd E[16][2];
  for (int ns = 0; ns < 16; ns++)
    for (int x = 0; x < 2; x++) {
      cd s = 0;
      for (int k = 0; k < LTAP; k++) s += h[k] * SYMTAB[ns][x][k];
      E[ns][x] = s;
    }
  static thread_local double G[BURST][16][2], A[BURST + 1][16];
  for (int m = 0; m < BURST; m++)
    for (int ns = 0; ns < 16; ns++)
      for (int x = 0; x < 2; x++) G[m][ns][x] = m < DLY ? 0 : std::norm(r[m - DLY] - E[ns][x]) / sigma2;
  for (int s = 0; s < 16; s++) A[0][s] = 0;
  for (int m = 0; m < BURST; m++) {
    double mn = 1e300;
    for (int ns = 0; ns < 16; ns++) {
      double a = std::min(A[m][P_[ns][0]] + G[m][ns][0], A[m][P_[ns][1]] + G[m][ns][1]);
      A[m + 1][ns] = a;
      mn = std::min(mn, a);
    }
    for (int ns = 0; ns < 16; ns++) A[m + 1][ns] -= mn;
  }
  double Bk[16] = {};
  for (int m = BURST - 1; m >= 0; m--) {
    double b0 = 1e300, b1 = 1e300;
    for (int ns = 0; ns < 16; ns++) {
      double t = std::min(A[m][P_[ns][0]] + G[m][ns][0], A[m][P_[ns][1]] + G[m][ns][1]) + Bk[ns];
      if (ns & 1)
        b1 = std::min(b1, t);
      else
        b0 = std::min(b0, t);
    }
    llr[m] = b1 - b0;
    double nb[16], mn = 1e300;
    for (int o = 0; o < 16; o++) {
      nb[o] = std::min(G[m][SUCC[o][0]][XOLD[o]] + Bk[SUCC[o][0]], G[m][SUCC[o][1]][XOLD[o]] + Bk[SUCC[o][1]]);
      mn = std::min(mn, nb[o]);
    }
    for (int o = 0; o < 16; o++) Bk[o] = nb[o] - mn;
  }
}

// burst near start: timing by least squares on the training sequence -> soft bits
static bool demod(const Stream& z, long start, const Est& e, int search, double* llr, long* s0o)
{
  long N = z.size();
  int no = e.obs.size();
  double best = 1e300;
  long bs = -1;
  cd bh[LTAP];
  for (int o = -search; o <= search; o++) {
    long s = start + o;
    if (s < 0 || s + 150 * SPS >= N) continue;
    cd h[LTAP] = {};
    for (int i = 0; i < LTAP; i++)
      for (int r = 0; r < no; r++) h[i] += e.P[i * no + r] * cd(z[s + SPS * e.obs[r]]);
    double num = 0, den = 0;
    for (int r = 0; r < no; r++) {
      cd y = z[s + SPS * e.obs[r]], p = 0;
      for (int k = 0; k < LTAP; k++) p += e.A[r * LTAP + k] * h[k];
      num += std::norm(y - p);
      den += std::norm(y);
    }
    double res = num / (den + 1e-30);
    if (res < best) {
      best = res;
      bs = s;
      std::copy(h, h + LTAP, bh);
    }
  }
  if (bs < 0) return false;
  cd r[BURST];
  double pw = 0;
  for (int m = 0; m < BURST; m++) {
    r[m] = z[bs + SPS * m];
    pw += std::norm(r[m]);
  }
  double sigma2 = std::max(best * pw / BURST, 1e-30);
  mlse(r, bh, sigma2, llr);
  *s0o = bs;
  return true;
}

// ------------------------------------------------------------------ channel coding

static int CV_OLD[2][16];
static int CV_SGN[2][16][2];

static void init_conv()
{
  for (int ns = 0; ns < 16; ns++) {
    CV_OLD[0][ns] = ns >> 1;
    CV_OLD[1][ns] = (ns >> 1) | 8;
    for (int x = 0; x < 2; x++) {
      int st = CV_OLD[x][ns], u = ns & 1;
      int c0 = u ^ ((st >> 2) & 1) ^ ((st >> 3) & 1);
      int c1 = u ^ (st & 1) ^ ((st >> 2) & 1) ^ ((st >> 3) & 1);
      CV_SGN[x][ns][0] = 1 - 2 * c0;
      CV_SGN[x][ns][1] = 1 - 2 * c1;
    }
  }
}

// soft Viterbi, rate 1/2 K=5, ends in state 0; c: > 0 means 0
static std::vector<uint8_t> conv_decode(const std::vector<double>& c)
{
  int n = c.size() / 2;
  double metric[16];
  for (int s = 0; s < 16; s++) metric[s] = 1e9;
  metric[0] = 0;
  std::vector<uint8_t> bp(n * 16);
  for (int k = 0; k < n; k++) {
    double nm[16], mn = 1e300;
    for (int ns = 0; ns < 16; ns++) {
      double c0 = metric[CV_OLD[0][ns]] - (CV_SGN[0][ns][0] * c[2 * k] + CV_SGN[0][ns][1] * c[2 * k + 1]);
      double c1 = metric[CV_OLD[1][ns]] - (CV_SGN[1][ns][0] * c[2 * k] + CV_SGN[1][ns][1] * c[2 * k + 1]);
      bool pick = c1 < c0;
      bp[k * 16 + ns] = pick;
      nm[ns] = pick ? c1 : c0;
      mn = std::min(mn, nm[ns]);
    }
    for (int s = 0; s < 16; s++) metric[s] = nm[s] - mn;
  }
  std::vector<uint8_t> u(n);
  int st = 0;
  for (int k = n - 1; k >= 0; k--) {
    u[k] = st & 1;
    st = (st >> 1) | (bp[k * 16 + st] << 3);
  }
  return u;
}

static bool parity_ok(const std::vector<uint8_t>& u, int ninfo, uint64_t poly, int nb)
{
  uint64_t reg = 0, mask = (nb == 64) ? ~0ULL : ((1ULL << nb) - 1);
  for (int i = 0; i < ninfo; i++) {
    int fb = ((reg >> (nb - 1)) & 1) ^ u[i];
    reg = (reg << 1) & mask;
    if (fb) reg ^= poly;
  }
  for (int i = 0; i < nb; i++)
    if ((1 - ((reg >> (nb - 1 - i)) & 1)) != u[ninfo + i]) return false;
  return true;
}

static std::vector<uint8_t> pack_lsb(const std::vector<uint8_t>& bits, int n)
{
  std::vector<uint8_t> out((n + 7) / 8, 0);
  for (int i = 0; i < n; i++)
    if (bits[i]) out[i / 8] |= 1 << (i % 8);
  return out;
}

static const uint64_t SCH_POLY = (1 << 8) | (1 << 6) | (1 << 5) | (1 << 4) | (1 << 2) | 1;
static const uint64_t FIRE_POLY = (1ULL << 26) | (1ULL << 23) | (1ULL << 17) | (1ULL << 3) | 1;

static bool decode_sch(const double* bits, int* bsic, long* fn)
{
  std::vector<double> c;
  c.insert(c.end(), bits + 3, bits + 42);
  c.insert(c.end(), bits + 106, bits + 145);
  auto u = conv_decode(c);
  if (!parity_ok(u, 25, SCH_POLY, 10)) return false;
  auto d = pack_lsb(u, 25);
  *bsic = (d[0] >> 2) & 0x3f;
  long t1 = ((d[0] & 3) << 9) | (d[1] << 1) | (d[2] >> 7);
  long t2 = (d[2] >> 2) & 0x1f;
  long t3 = (((d[2] & 3) << 1) | (d[3] & 1)) * 10 + 1;
  *fn = 51 * (((t3 - t2) % 26 + 26) % 26) + t3 + 51 * 26 * t1;
  return true;
}

static bool decode_xcch(const double* b[4], std::vector<uint8_t>* msg)
{
  std::vector<double> c(456);
  for (int k = 0; k < 456; k++) {
    int B = k % 4, j = 2 * ((49 * k) % 57) + ((k % 8) / 4);
    int pos = j < 57 ? 3 + j : 88 + (j - 57);
    c[k] = b[B][pos];
  }
  auto u = conv_decode(c);
  if (!parity_ok(u, 184, FIRE_POLY, 40)) return false;
  *msg = pack_lsb(u, 184);
  return true;
}

// ------------------------------------------------------------------ per channel

struct Result {
  int arfcn;
  int fcch = 0;
  double df = 0, level = -200;
  int bsic = -1;
  std::vector<std::pair<int, std::string>> msgs;  // (tc, hex)
};

static Est EST_SCH, EST_TSC[8];

static inline long floordiv(long a, long b) { return a >= 0 ? a / b : -((-a + b - 1) / b); }

static Result decode_channel(int arfcn, const Stream& z)
{
  Result res;
  res.arfcn = arfcn;
  double p = 0;
  for (auto& v : z) p += std::norm(v);
  res.level = 10 * std::log10(p / std::max<size_t>(1, z.size()) + 1e-20);
  double df;
  auto hits = find_fcch(z, &df);
  res.fcch = hits.size();
  res.df = df;
  if (hits.empty()) return res;
  long N = z.size();
  Stream y(N);
  Phasor ph(df / FSC);
  for (long n = 0; n < N; n++) y[n] = z[n] * ROT8[n & 7] * cf(ph.next());
  // two SCH that agree (BSIC, frame numbers vs. time)
  std::vector<std::pair<int, std::pair<long, long>>> got;  // bsic, (fn, s0)
  bool ok = false;
  long fn0 = 0, s00 = 0;
  double llr[BURST];
  for (size_t i = 0; i < hits.size() && i < 12 && !ok; i++) {
    long s0;
    if (!demod(y, hits[i] + FRAME, EST_SCH, 12, llr, &s0)) continue;
    int bsic;
    long fn;
    if (!decode_sch(llr, &bsic, &fn)) continue;
    for (auto& g : got) {
      long dfn = ((fn - g.second.first) % HYPER + HYPER) % HYPER;
      long dt = ((lround((double)(s0 - g.second.second) / FRAME)) % HYPER + HYPER) % HYPER;
      if (g.first == bsic && dfn == dt) {
        ok = true;
        res.bsic = bsic;
        fn0 = fn;
        s00 = s0;
      }
    }
    got.push_back({bsic, {fn, s0}});
  }
  if (!ok) return res;
  const Est& e = EST_TSC[res.bsic & 7];
  long first = fn0 - fn0 % 51 - 51 * (floordiv(s00, FRAME) / 51 + 1);
  std::set<std::string> seen;
  for (long base = first; base < first + 51 * 40; base += 51) {
    long st[4];
    for (int f = 0; f < 4; f++) st[f] = s00 + (base + 2 + f - fn0) * FRAME;
    if (st[0] < 20 || st[3] + 160 * SPS > N) continue;
    static thread_local double bb[4][BURST];
    const double* b[4];
    bool all = true;
    for (int f = 0; f < 4 && all; f++) {
      long s0;
      all = demod(y, st[f], e, 4, bb[f], &s0);
      b[f] = bb[f];
    }
    if (!all) continue;
    std::vector<uint8_t> msg;
    if (!decode_xcch(b, &msg)) continue;
    char hex[64];
    for (int i = 0; i < 23; i++) sprintf(hex + 2 * i, "%02x", msg[i]);
    if (seen.insert(hex).second) res.msgs.push_back({(int)(((base / 51) % 8 + 8) % 8), hex});
  }
  return res;
}

// ------------------------------------------------------------------ main

static std::vector<Result> run_parallel(const std::map<int, Stream>& ch, int threads, bool fcch_only)
{
  std::vector<int> keys;
  for (auto& kv : ch) keys.push_back(kv.first);
  std::vector<Result> out(keys.size());
  std::atomic<size_t> next(0);
  auto work = [&]() {
    for (size_t i; (i = next++) < keys.size();) {
      const auto& z = ch.at(keys[i]);
      if (fcch_only) {
        double df;
        out[i].arfcn = keys[i];
        out[i].fcch = find_fcch(z, &df).size();
      } else {
        out[i] = decode_channel(keys[i], z);
      }
    }
  };
  std::vector<std::thread> th;
  for (int t = 0; t < threads; t++) th.emplace_back(work);
  for (auto& t : th) t.join();
  return out;
}

int main(int argc, char** argv)
{
  const char* path = nullptr;
  double fc = 0, fs = 56e6, bw = 0, secs = 0, pre = 0.4;
  bool int8 = false;
  int threads = std::max(1u, std::thread::hardware_concurrency());
  std::vector<int> arfcns;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto val = [&]() { return i + 1 < argc ? argv[++i] : (char*)""; };
    if (a == "-i") path = val();
    else if (a == "-f") fc = atof(val());
    else if (a == "-r") fs = atof(val());
    else if (a == "-w") bw = atof(val());
    else if (a == "-j") threads = atoi(val());
    else if (a == "--secs") secs = atof(val());
    else if (a == "--presearch") pre = atof(val());
    else if (a == "--fcch-max-hz") FCCH_MAX_HZ = atof(val());
    else if (a == "--int8") int8 = true;
    else if (a == "-a") {
      char* s = val();
      for (char* t = strtok(s, " ,"); t; t = strtok(nullptr, " ,")) arfcns.push_back(atoi(t));
    } else {
      fprintf(stderr, "usage: gsm_decoder -i file.iq -f centre_hz -r rate [-w bw] -a \"arfcns\" [--int8] "
                      "[--secs S] [--presearch S] [--fcch-max-hz F] [-j threads]\n");
      return 2;
    }
  }
  if (!path || fc == 0 || arfcns.empty()) {
    fprintf(stderr, "gsm_decoder: -i, -f and -a are required\n");
    return 2;
  }
  if (bw == 0) bw = 0.8 * fs;
  init_tables();
  init_conv();
  EST_SCH = make_est(SCH_TRAIN, 42);
  for (int t = 0; t < 8; t++) EST_TSC[t] = make_est(TSC[t], 61);
  Capture cap;
  if (!cap.open(path, int8)) {
    fprintf(stderr, "gsm_decoder: cannot open %s\n", path);
    return 1;
  }
  auto t0 = std::chrono::steady_clock::now();
  auto secs_since = [&]() { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };
  if (pre > 0 && arfcns.size() > 8) {
    // FCCH look on a short part first: most channels carry no BCCH
    auto shortc = channelize(cap, fs, fc, bw, arfcns, pre, threads);
    auto r = run_parallel(shortc, threads, true);
    std::vector<int> found;
    for (auto& x : r)
      if (x.fcch >= 3) found.push_back(x.arfcn);
    fprintf(stderr, "FCCH on %zu of %zu channels (%.1f s)\n", found.size(), shortc.size(), secs_since());
    arfcns = found;
  }
  if (arfcns.empty()) return 0;
  auto ch = channelize(cap, fs, fc, bw, arfcns, secs, threads);
  double tc = secs_since();
  auto res = run_parallel(ch, threads, false);
  fprintf(stderr, "channelised %zu in %.1f s, decoded in %.1f s\n", ch.size(), tc, secs_since() - tc);
  for (auto& r : res) {
    if (r.bsic < 0) continue;
    printf("{\"arfcn\": %d, \"bsic\": %d, \"fcch\": %d, \"df_hz\": %ld, \"level_dbfs\": %.1f, \"msgs\": [", r.arfcn,
           r.bsic, r.fcch, lround(r.df), r.level);
    for (size_t i = 0; i < r.msgs.size(); i++)
      printf("%s{\"tc\": %d, \"hex\": \"%s\"}", i ? ", " : "", r.msgs[i].first, r.msgs[i].second.c_str());
    printf("]}\n");
  }
  fflush(stdout);
  return 0;
}
