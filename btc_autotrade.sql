-- phpMyAdmin SQL Dump
-- version 5.2.0
-- https://www.phpmyadmin.net/
--
-- Host: 127.0.0.1
-- Generation Time: Aug 14, 2025 at 06:11 AM
-- Server version: 10.4.27-MariaDB
-- PHP Version: 8.1.6

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;

--
-- Database: `btc_autotrade`
--

-- --------------------------------------------------------

--
-- Table structure for table `inr_wallet_transactions`
--

CREATE TABLE `inr_wallet_transactions` (
  `id` int(11) NOT NULL,
  `trade_time` datetime DEFAULT NULL,
  `action` varchar(50) DEFAULT NULL,
  `amount` double DEFAULT NULL,
  `balance_after` double DEFAULT NULL,
  `trade_mode` varchar(10) DEFAULT 'TEST',
  `payment_id` varchar(255) DEFAULT NULL,
  `status` varchar(20) NOT NULL,
  `reversal_id` varchar(50) NOT NULL,
  `razorpay_order_id` varchar(50) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `live_trades`
--

CREATE TABLE `live_trades` (
  `id` int(11) NOT NULL,
  `trade_time` datetime DEFAULT NULL,
  `order_id` varchar(50) DEFAULT NULL,
  `action` varchar(10) DEFAULT NULL,
  `amount` double DEFAULT NULL,
  `price` double DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  `profit` double DEFAULT NULL,
  `reason` varchar(50) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `payout_logs`
--

CREATE TABLE `payout_logs` (
  `id` int(11) NOT NULL,
  `recipient_name` varchar(100) DEFAULT NULL,
  `method` enum('bank','upi') DEFAULT NULL,
  `fund_account_id` varchar(50) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `status` varchar(50) DEFAULT NULL,
  `razorpay_payout_id` varchar(50) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp()
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `razorpay_payment_log`
--

CREATE TABLE `razorpay_payment_log` (
  `id` int(11) NOT NULL,
  `order_id` varchar(100) DEFAULT NULL,
  `customer_id` varchar(50) DEFAULT NULL,
  `name` varchar(50) DEFAULT NULL,
  `method` varchar(50) DEFAULT NULL,
  `account_number` varchar(50) DEFAULT NULL,
  `ifsc` varchar(50) DEFAULT NULL,
  `upi_id` varchar(50) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `status` varchar(100) DEFAULT NULL,
  `response` varchar(100) DEFAULT NULL,
  `credited_at` timestamp NOT NULL DEFAULT current_timestamp(),
  `retry_count` int(11) NOT NULL,
  `last_attempt_time` datetime DEFAULT current_timestamp()
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `saved_recipients`
--

CREATE TABLE `saved_recipients` (
  `id` int(11) NOT NULL,
  `name` varchar(100) DEFAULT NULL,
  `method` varchar(20) DEFAULT NULL,
  `account_number` varchar(50) DEFAULT NULL,
  `ifsc` varchar(20) DEFAULT NULL,
  `upi_id` varchar(50) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `saved_upi_recipients`
--

CREATE TABLE `saved_upi_recipients` (
  `id` int(11) NOT NULL,
  `name` varchar(100) DEFAULT NULL,
  `email` varchar(100) DEFAULT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `upi_id` varchar(100) DEFAULT NULL,
  `contact_id` varchar(50) DEFAULT NULL,
  `fund_account_id` varchar(50) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `user_wallets`
--

CREATE TABLE `user_wallets` (
  `user_email` varchar(100) NOT NULL,
  `inr_balance` decimal(10,2) DEFAULT 0.00,
  `customer_id` varchar(255) NOT NULL,
  `id` int(11) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `wallet_history`
--

CREATE TABLE `wallet_history` (
  `id` int(11) NOT NULL,
  `trade_date` date DEFAULT NULL,
  `start_balance` double DEFAULT NULL,
  `end_balance` double DEFAULT NULL,
  `current_inr_value` double DEFAULT NULL,
  `trade_count` int(11) DEFAULT NULL,
  `auto_start_price` double NOT NULL,
  `auto_end_price` double DEFAULT NULL,
  `auto_profit` double NOT NULL,
  `total_deposit_inr` double DEFAULT 0,
  `total_btc_received` double DEFAULT 0,
  `total_btc_sent` double DEFAULT 0,
  `profit_inr` double DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------

--
-- Table structure for table `wallet_transactions`
--

CREATE TABLE `wallet_transactions` (
  `id` int(11) NOT NULL,
  `trade_time` datetime DEFAULT NULL,
  `action` varchar(20) DEFAULT NULL,
  `amount` double DEFAULT NULL,
  `balance_after` double DEFAULT NULL,
  `inr_value` double DEFAULT NULL,
  `trade_type` varchar(200) DEFAULT 'MANUAL',
  `autotrade_active` int(11) NOT NULL,
  `status` varchar(20) NOT NULL,
  `reversal_id` varchar(50) NOT NULL,
  `is_autotrade_marker` tinyint(1) DEFAULT 0,
  `last_price` double DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Indexes for dumped tables
--

--
-- Indexes for table `inr_wallet_transactions`
--
ALTER TABLE `inr_wallet_transactions`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `live_trades`
--
ALTER TABLE `live_trades`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `payout_logs`
--
ALTER TABLE `payout_logs`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `razorpay_payment_log`
--
ALTER TABLE `razorpay_payment_log`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `saved_recipients`
--
ALTER TABLE `saved_recipients`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `saved_upi_recipients`
--
ALTER TABLE `saved_upi_recipients`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `user_wallets`
--
ALTER TABLE `user_wallets`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `wallet_history`
--
ALTER TABLE `wallet_history`
  ADD PRIMARY KEY (`id`);

--
-- Indexes for table `wallet_transactions`
--
ALTER TABLE `wallet_transactions`
  ADD PRIMARY KEY (`id`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `inr_wallet_transactions`
--
ALTER TABLE `inr_wallet_transactions`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `live_trades`
--
ALTER TABLE `live_trades`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `payout_logs`
--
ALTER TABLE `payout_logs`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `razorpay_payment_log`
--
ALTER TABLE `razorpay_payment_log`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `saved_recipients`
--
ALTER TABLE `saved_recipients`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `saved_upi_recipients`
--
ALTER TABLE `saved_upi_recipients`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `user_wallets`
--
ALTER TABLE `user_wallets`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `wallet_history`
--
ALTER TABLE `wallet_history`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `wallet_transactions`
--
ALTER TABLE `wallet_transactions`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;
COMMIT;

/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
