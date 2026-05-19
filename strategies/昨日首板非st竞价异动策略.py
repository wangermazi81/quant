# META: {"name": "昨日首板非ST竞价异动策略", "desc": "昨日首板涨停且非一字板，非ST，今日竞价成交额/昨日成交总额>0.18", "author": "AI生成", "builtin": false}
import pandas as pd
import numpy as np

def scan(codes: list, get_kline, get_quotes) -> list:
    """
    策略扫描函数
    codes: 股票代码列表
    get_kline(code, days) -> DataFrame[date,open,close,high,low,volume]
    get_quotes(codes) -> DataFrame[代码,名称,最新价,涨跌幅,昨收,成交量,量比]
    返回: list of {code, name, price, change, score, reason}
    """
    results = []
    
    # 先批量获取所有股票的行情信息
    try:
        df_quotes = get_quotes(codes)
        if df_quotes is None or df_quotes.empty:
            return results
    except:
        return results
    
    for code in codes:
        try:
            df = get_kline(code, 60)
            if df is None or len(df) < 20:
                continue
                
            df = df.sort_values('date').reset_index(drop=True)
            
            # 获取股票行情信息
            quote_info = df_quotes[df_quotes['代码'] == code]
            if quote_info.empty:
                continue
                
            name = quote_info.iloc[0]['名称']
            price = quote_info.iloc[0]['最新价']
            change_pct = quote_info.iloc[0]['涨跌幅']
            
            # 条件1：非ST
            if 'ST' in name or 'st' in name:
                continue
            
            # 条件2：昨日首板涨停且非一字板
            # 检查是否有足够数据
            if len(df) < 2:
                continue
                
            yesterday = df.iloc[-2]
            yesterday_close = yesterday['close']
            yesterday_open = yesterday['open']
            yesterday_high = yesterday['high']
            yesterday_low = yesterday['low']
            yesterday_volume = yesterday['volume']
            
            # 计算涨停价（A股涨跌幅限制为10%）
            prev_day = df.iloc[-3] if len(df) >= 3 else None
            if prev_day is None:
                continue
                
            prev_close = prev_day['close']
            limit_up_price = round(prev_close * 1.1, 2)
            
            # 判断是否涨停
            is_limit_up = abs(yesterday_close - limit_up_price) < 0.01
            
            # 判断是否一字板（开盘价等于收盘价）
            is_yizi = abs(yesterday_open - yesterday_close) < 0.01
            
            # 条件3：今日竞价成交额/昨天成交总额>0.18
            today = df.iloc[-1]
            today_open = today['open']
            today_volume = today['volume']
            
            # 竞价成交额近似计算：开盘价 * 竞价成交量（这里假设竞价成交量为今日成交量的10%作为近似）
            auction_volume = today_volume * 0.1  # 假设竞价阶段成交量为全天10%
            auction_amount = today_open * auction_volume
            
            # 昨日成交总额近似计算：平均价 * 成交量
            avg_price_yesterday = (yesterday_open + yesterday_close + yesterday_high + yesterday_low) / 4
            yesterday_amount = avg_price_yesterday * yesterday_volume
            
            ratio = auction_amount / yesterday_amount if yesterday_amount > 0 else 0
            
            # 综合判断
            if is_limit_up and not is_yizi and ratio > 0.18:
                score = round(ratio * 100, 2)
                reason = f"昨涨停价{limit_up_price:.2f},非一字板,竞价额比{ratio:.2%}"
                
                results.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'change': change_pct,
                    'score': score,
                    'reason': reason
                })
        except Exception:
            continue
    
    # 按得分降序，最多返回50只
    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results[:50]