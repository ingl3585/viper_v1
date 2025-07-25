"""
Volatility Carry Strategy
Trades volatility term structure and carry opportunities.
"""
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import numpy as np

from src.strategies.base_strategy import BaseStrategy, Signal
from src.infra.nt_bridge import MarketData
from src.config import SystemConfig


class VolatilityCarryStrategy(BaseStrategy):
    """Volatility carry strategy based on vol term structure and carry opportunities."""
    
    def __init__(self, config, system_config: SystemConfig, price_history_manager=None):
        super().__init__("VolatilityCarry", config, system_config, price_history_manager)
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Carry tracking
        self.vol_term_structure = {}  # Store volatility term structure
        self.carry_history = []  # Track carry opportunities
        self.last_carry_signal = None
        self.carry_threshold_breached = False
        
    def generate_signal(self, market_data: MarketData) -> Optional[Signal]:
        """Generate volatility carry signal."""
        try:
            # Update price history first
            self.update_price_history(market_data)
            
            # Check if we have sufficient data
            if not self._has_sufficient_data():
                self.logger.info("VolCarry: Insufficient data for analysis")
                return None
            
            # Calculate term structure signals
            primary_signal = self._analyze_term_structure(market_data)
            if not primary_signal:
                self.logger.info("VolCarry: No term structure signal generated")
                return None
            
            # Apply carry filters
            if not self._validate_carry_opportunity(primary_signal, market_data):
                self.logger.info("VolCarry: Carry opportunity validation failed")
                return None
            
            # Apply risk management
            if not self.should_trade(market_data):
                self.logger.info("VolCarry: Risk management blocked trade")
                return None
            
            # Check confidence threshold
            if primary_signal.confidence < self.system_config.risk_management.min_confidence:
                self.logger.info(f"VolCarry: Signal confidence {primary_signal.confidence:.3f} below minimum {self.system_config.risk_management.min_confidence}")
                return None
                
            self.logger.info(f"VolCarry: Generated {['HOLD', 'BUY', 'SELL'][primary_signal.action]} signal with confidence {primary_signal.confidence:.3f}")
            return primary_signal
            
        except Exception as e:
            self.logger.error(f"VolCarry: Error generating signal: {e}")
            return None
    
    def _has_sufficient_data(self) -> bool:
        """Check if we have sufficient data for analysis."""
        status = self.price_history_manager.get_status()
        
        # Need at least 100 data points for each timeframe for term structure analysis
        required_data = {
            '5m': 100,  # 8+ hours
            '15m': 100,  # 25+ hours  
            '1h': 100    # 4+ days
        }
        
        for timeframe, required in required_data.items():
            if status.get(timeframe, {}).get('data_points', 0) < required:
                return False
        
        return True
    
    def _analyze_term_structure(self, market_data: MarketData) -> Optional[Signal]:
        """Analyze volatility term structure for carry opportunities."""
        try:
            # Calculate volatility for different timeframes (proxying for term structure)
            timeframes = ['5m', '15m', '1h']
            vol_structure = {}
            
            for timeframe in timeframes:
                realized_vols = self.price_history_manager.calculate_realized_volatility(
                    timeframe, (20, 60)  # Short and medium term
                )
                if realized_vols:
                    vol_structure[timeframe] = realized_vols
            
            if len(vol_structure) < 3:
                return None
                
            # Extract volatilities for term structure analysis
            short_vol = vol_structure['5m'].get('20_period', 0.0)   # Short term realized
            medium_vol = vol_structure['15m'].get('20_period', 0.0)  # Medium term realized
            long_vol = vol_structure['1h'].get('20_period', 0.0)    # Long term realized
            
            # Calculate term structure slope
            # Positive slope = contango (long vol > short vol) - bearish for vol
            # Negative slope = backwardation (short vol > long vol) - bullish for vol
            short_medium_slope = (medium_vol - short_vol) / short_vol if short_vol > 0 else 0
            medium_long_slope = (long_vol - medium_vol) / medium_vol if medium_vol > 0 else 0
            overall_slope = (long_vol - short_vol) / short_vol if short_vol > 0 else 0
            
            # Volatility carry signal logic
            signal = None
            confidence = 0.0
            current_price = market_data.current_price
            
            # Strong contango - sell volatility (expect vol to decrease)
            if overall_slope > self.config.contango_threshold:
                signal_strength = min(overall_slope / self.config.contango_threshold, 2.0)
                confidence = min(0.5 + signal_strength * 0.2, 0.9)
                
                # Calculate stop and target based on volatility
                atr = self.calculate_atr_simple(market_data.price_15m[-20:]) if len(market_data.price_15m) >= 20 else current_price * 0.01
                stop_price = current_price + (atr * self.system_config.risk_management.stop_loss_atr_multiplier)
                target_price = current_price - (atr * self.config.target_atr_multiplier)
                
                signal = Signal(
                    action=2,  # SELL
                    confidence=confidence,
                    entry_price=current_price,
                    stop_price=stop_price,
                    target_price=target_price,
                    reason=f"Vol carry: Strong contango (slope={overall_slope:.3f})"
                )
            
            # Strong backwardation - buy volatility (expect vol to increase)
            elif overall_slope < -self.config.backwardation_threshold:
                signal_strength = min(abs(overall_slope) / self.config.backwardation_threshold, 2.0)
                confidence = min(0.5 + signal_strength * 0.2, 0.9)
                
                # Calculate stop and target
                atr = self.calculate_atr_simple(market_data.price_15m[-20:]) if len(market_data.price_15m) >= 20 else current_price * 0.01
                stop_price = current_price - (atr * self.system_config.risk_management.stop_loss_atr_multiplier)
                target_price = current_price + (atr * self.config.target_atr_multiplier)
                
                signal = Signal(
                    action=1,  # BUY
                    confidence=confidence,
                    entry_price=current_price,
                    stop_price=stop_price,
                    target_price=target_price,
                    reason=f"Vol carry: Strong backwardation (slope={overall_slope:.3f})"
                )
            
            # Log term structure analysis
            self.logger.info(f"VolCarry Term Structure Analysis:")
            self.logger.info(f"  Short vol (5m): {short_vol:.4f}")
            self.logger.info(f"  Medium vol (15m): {medium_vol:.4f}")
            self.logger.info(f"  Long vol (1h): {long_vol:.4f}")
            self.logger.info(f"  Overall slope: {overall_slope:.4f}")
            self.logger.info(f"  Signal: {signal.action if signal else 'None'}")
            
            return signal
            
        except Exception as e:
            self.logger.error(f"VolCarry: Error in term structure analysis: {e}")
            return None
    
    def _validate_carry_opportunity(self, signal: Signal, market_data: MarketData) -> bool:
        """Validate that this is a genuine carry opportunity."""
        try:
            # Check volatility regime - carry works better in certain regimes
            vol_regime = self.price_history_manager.calculate_volatility_regime('15m')
            
            # Carry opportunities are typically better in:
            # - Medium volatility regimes (not too low, not too high)
            # - When volatility is mean-reverting rather than trending
            
            if vol_regime == 'low' and signal.action == 2:  # Selling vol in low vol regime is risky
                return False
            
            if vol_regime == 'high' and signal.action == 1:  # Buying vol in high vol regime is risky
                return False
            
            # Check for volatility breakouts that might invalidate carry
            breakout_info = self.price_history_manager.calculate_volatility_breakout('15m')
            if breakout_info and breakout_info.get('is_breakout', False):
                # Strong volatility breakouts can overwhelm carry signals
                if breakout_info.get('breakout_strength', 0) > 2.5:
                    return False
            
            # Check minimum carry threshold
            if signal.confidence < self.config.min_carry_confidence:
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"VolCarry: Error validating carry opportunity: {e}")
            return False
    
    def get_strategy_status(self) -> Dict[str, Any]:
        """Get detailed strategy status."""
        try:
            vol_metrics = self.calculate_volatility_metrics(None)
            
            return {
                'name': self.name,
                'last_signal': self.last_carry_signal,
                'term_structure': self.vol_term_structure,
                'carry_history_length': len(self.carry_history),
                'volatility_metrics': vol_metrics,
                'threshold_breached': self.carry_threshold_breached
            }
        except Exception as e:
            self.logger.error(f"VolCarry: Error getting strategy status: {e}")
            return {'name': self.name, 'status': 'error'}