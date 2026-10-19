/** 预测页「模块化」元数据：流水线阶段说明、特征分组、算法超参规格。
 *
 * 预测页把训练流水线显式拆成「特征工程 → 标签构建 → 时序切分 → 模型训练 →
 * 样本外评估 → 打分预测」六个阶段，逐段展示功能说明并允许即时自定义（特征子集、
 * 算法超参、测试集占比），训练时随请求发往后端，不落库、不影响默认链路。
 *
 * 数据来源约定：
 * - 特征名与分组需覆盖 `plugins/predict/services/dataset.py::FEATURE_COLUMNS`；
 * - 超参范围与 `plugins/predict/services/models.py::PARAM_SPECS` 一致——前端据此
 *   提前给出输入约束与默认值，最终仍由后端白名单校验。
 */

export interface PipelineModule {
  id: string;
  index: string;
  title: string;
  desc: string;
}

/** 流水线阶段总览（顺序即数据流顺序，用于顶部导航条与模块说明）。 */
export const PIPELINE_MODULES: PipelineModule[] = [
  {
    id: "feature",
    index: "①",
    title: "特征工程",
    desc: "把原始行情计算成 point-in-time 特征向量；勾掉的特征不会进入模型输入。",
  },
  {
    id: "label",
    index: "②",
    title: "标签构建",
    desc: "以 horizon 为持有期，用未来收益的正负生成 0/1 标签；每个标的最后 horizon 行无标签会被丢弃。",
  },
  {
    id: "split",
    index: "③",
    title: "时序切分",
    desc: "按日历先后切分训练/测试集，并净化（purge）标签跨越边界的训练尾部，避免未来信息泄漏。",
  },
  {
    id: "model",
    index: "④",
    title: "模型训练",
    desc: "在训练集上拟合所选算法；超参只改变拟合方式，不影响特征与标签的定义。",
  },
  {
    id: "evaluate",
    index: "⑤",
    title: "样本外评估",
    desc: "在从未参与训练的测试集上计算 AUC/IC/分层收益，并与随机基线对照，判断信号是否真实。",
  },
  {
    id: "score",
    index: "⑥",
    title: "打分预测",
    desc: "用生效模型对当前宇宙逐标的打分，输出上涨概率；打分不改变模型，可重复运行。",
  },
];

export interface FeatureItem {
  name: string;
  label: string;
}

export interface FeatureGroup {
  key: string;
  label: string;
  desc: string;
  features: FeatureItem[];
}

/** 特征的语义分组；`name` 必须与后端 `FEATURE_COLUMNS` 完全一致。 */
export const FEATURE_GROUPS: FeatureGroup[] = [
  {
    key: "momentum",
    label: "价格与动量",
    desc: "短期收益与中长周期动量，刻画趋势方向与强度。",
    features: [
      { name: "ret_1", label: "1 日收益" },
      { name: "ret_5", label: "5 日收益" },
      { name: "ret_20", label: "20 日收益" },
      { name: "mom_20", label: "20 日动量" },
      { name: "mom_60", label: "60 日动量" },
      { name: "mom_120", label: "120 日动量" },
    ],
  },
  {
    key: "ma_gap",
    label: "均线乖离",
    desc: "收盘价相对 5/20/60 日均线的偏离，衡量超买超卖与均值回归压力。",
    features: [
      { name: "ma5_gap", label: "5 日均线乖离" },
      { name: "ma20_gap", label: "20 日均线乖离" },
      { name: "ma60_gap", label: "60 日均线乖离" },
    ],
  },
  {
    key: "technical",
    label: "技术指标",
    desc: "MACD / RSI / KDJ / BOLL 等经典指标的当前状态。",
    features: [
      { name: "macd_dif_n", label: "MACD DIF（归一）" },
      { name: "macd_hist_n", label: "MACD 柱（归一）" },
      { name: "rsi_14", label: "RSI(14)" },
      { name: "kdj_k", label: "KDJ K" },
      { name: "kdj_d", label: "KDJ D" },
      { name: "boll_pos", label: "布林通道位置" },
    ],
  },
  {
    key: "volatility",
    label: "波动率",
    desc: "ATR 与滚动波动率，刻画风险水平与振幅。",
    features: [
      { name: "atr_pct", label: "ATR 波幅占比" },
      { name: "vol_20", label: "20 日波动率" },
      { name: "vol_60", label: "60 日波动率" },
    ],
  },
  {
    key: "liquidity",
    label: "流动性",
    desc: "量比、换手、成交额与 Amihud 非流动性，刻画可交易性。",
    features: [
      { name: "vol_ratio_log", label: "量比（对数）" },
      { name: "turnover_20", label: "20 日换手率" },
      { name: "amount_log_20", label: "成交额（对数）" },
      { name: "amihud_20", label: "Amihud 非流动性" },
    ],
  },
  {
    key: "position",
    label: "形态与位置",
    desc: "均线排列、突破与距高点距离，刻画形态与相对位置。",
    features: [
      { name: "ma_bull", label: "均线多头排列" },
      { name: "breakout_20", label: "20 日突破" },
      { name: "dist_high_252", label: "距 52 周高点" },
    ],
  },
];

/** 全部可选特征（顺序即后端规范顺序）。 */
export const ALL_FEATURES: string[] = FEATURE_GROUPS.flatMap((group) =>
  group.features.map((feature) => feature.name),
);

export interface AlgoParamSpec {
  name: string;
  label: string;
  kind: "int" | "float";
  min: number;
  max: number;
  step: number;
  /** 后端内置默认值（未自定义时生效）。 */
  default: number;
  desc: string;
}

export const ALGO_OPTIONS = [
  { value: "hist_gbdt", label: "梯度提升 (HistGBDT)" },
  { value: "random_forest", label: "随机森林" },
  { value: "logistic", label: "逻辑回归 (基线)" },
];

/** 各算法的可调超参；范围与后端 PARAM_SPECS 对齐。 */
export const ALGO_PARAM_SPECS: Record<string, AlgoParamSpec[]> = {
  hist_gbdt: [
    {
      name: "max_iter",
      label: "迭代轮数 max_iter",
      kind: "int",
      min: 10,
      max: 5000,
      step: 10,
      default: 300,
      desc: "提升轮数越多拟合越充分，也越容易过拟合。",
    },
    {
      name: "learning_rate",
      label: "学习率 learning_rate",
      kind: "float",
      min: 0.0001,
      max: 1.0,
      step: 0.01,
      default: 0.05,
      desc: "每棵树的贡献权重；越小需要越多轮数。",
    },
    {
      name: "max_depth",
      label: "树最大深度 max_depth",
      kind: "int",
      min: 1,
      max: 32,
      step: 1,
      default: 4,
      desc: "单棵树深度上限，越深越能拟合非线性交互。",
    },
    {
      name: "min_samples_leaf",
      label: "叶节点最小样本 min_samples_leaf",
      kind: "int",
      min: 1,
      max: 10000,
      step: 1,
      default: 50,
      desc: "叶节点最小样本数，越大越保守、越不易过拟合。",
    },
    {
      name: "l2_regularization",
      label: "L2 正则 l2_regularization",
      kind: "float",
      min: 0.0,
      max: 100.0,
      step: 0.1,
      default: 1.0,
      desc: "叶子权重的 L2 惩罚强度。",
    },
  ],
  random_forest: [
    {
      name: "n_estimators",
      label: "树数量 n_estimators",
      kind: "int",
      min: 10,
      max: 2000,
      step: 10,
      default: 300,
      desc: "森林中树的数量，越多越稳定但越慢。",
    },
    {
      name: "max_depth",
      label: "树最大深度 max_depth",
      kind: "int",
      min: 1,
      max: 64,
      step: 1,
      default: 8,
      desc: "单棵树深度上限。",
    },
    {
      name: "min_samples_leaf",
      label: "叶节点最小样本 min_samples_leaf",
      kind: "int",
      min: 1,
      max: 10000,
      step: 1,
      default: 50,
      desc: "叶节点最小样本数。",
    },
  ],
  logistic: [
    {
      name: "C",
      label: "正则强度倒数 C",
      kind: "float",
      min: 0.000001,
      max: 1000.0,
      step: 0.1,
      default: 0.5,
      desc: "C 越小正则越强；基线模型，便于对照。",
    },
    {
      name: "max_iter",
      label: "最大迭代 max_iter",
      kind: "int",
      min: 100,
      max: 20000,
      step: 100,
      default: 2000,
      desc: "求解器最大迭代次数。",
    },
  ],
};

/** 各算法的默认超参草稿（字符串形式，便于输入框绑定）。 */
export const DEFAULT_ALGO_PARAMS: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(ALGO_PARAM_SPECS).map(([algo, specs]) => [
    algo,
    Object.fromEntries(specs.map((spec) => [spec.name, String(spec.default)])),
  ]),
);
